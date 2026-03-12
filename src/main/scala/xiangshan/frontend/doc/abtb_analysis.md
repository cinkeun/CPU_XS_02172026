# abtb (AheadBtb) analysis

> Analysis criteria: BTB_analysis_rule.md
> Analysis target: `src/main/scala/xiangshan/frontend/bpu/abtb/`
> Code-based only — No use of prior knowledge/web-search

---

## 1.1 BTB types and roles

AheadBtb is **Block-type BTB**, which predicts the taken branch candidates (ways) of the fetch block.
The key point is not that “tag compare is fast,” but that **SRAM read + compare/select are pipelined in 2-stage**.

ABTB internal timing (PC_A -> PC_B perspective):
1. Cycle N (abtb s0): SRAM read request with `set/bank = f(PC_A)`
2. Cycle N+1 (abtb s1): Receive SRAM response (`entries@PC_A`), simultaneously latching `io.startPc` (=PC_B) for s2
3. Cycle N+2 (abtb s2): Compare `tag = getTag(PC_B)` and `entries@PC_A` and output prediction after hit/select

In other words, **tag matching itself is s2 combination 1 cycle**, but **SRAM read 1 cycle** is required before that.
ABTB internal latency is 2 cycles (s0->s1->s2).
The meaning of `ahead` is not to reduce latency to 1, but to match **throughput 1/cycle** by overlapping the read stage one cycle in front.

Prediction distance (reference PC must be specified to avoid confusion):
- **Based on BPU s0 input PC (PC_A)**: **two-block-ahead nature leading to `PC_A -> PC_B -> PC_C`**
- **Based on current block (PC_B) of BPU s1**: `PC_B -> PC_C` prediction, so **non-lookahead**

In other words, “non-lookahead” is established only based on the current-block of s1.

```
// Source: abtb/AheadBtb.scala:106-116 (s0 stage)
private val s0_previousStartPc = io.startPc // PC to create read address (based on previous block)
private val s0_setIdx   = getSetIndex(s0_previousStartPc)
private val s0_bankIdx  = getBankIndex(s0_previousStartPc)
// → Start SRAM read from s0 to s0_previousStartPc

// Source: abtb/AheadBtb.scala:124 (s1 stage)
private val s1_startPc = io.startPc // live PC of current cycle (for tag comparison in s2)
// → tag comparison is performed from s2 to s2_startPc (= RegEnable(s1_startPc))
```

| BTB | Type | Block Width | Predict Distance | Description |
|------|--------|------------------------|-------------------------------|------|
| abtb | Block | FetchBlockSize (bytes) | Based on s0(PC_A): two-block-ahead / Based on s1(PC_B): Current block → next block | 1024-entry, SRAM-based, lookahead-read |

---

## 1.2 BTB memory spec

### SRAM (per bank, AheadBtbBank)

```scala
// Source: abtb/AheadBtbBank.scala:37-50
private val sram = Module(new SplittedSRAMTemplate(
  new AheadBtbEntry,
  set = NumSets,      // 32 sets (= NumEntries / NumWays / NumBanks = 1024 / 8 / 4)
  way = NumWays,      // 8 ways
  waySplit = NumWays / 2,  // 4 (split for timing)
  dataSplit = 1,
  shouldReset = true,
  singlePort = true,
  holdRead = true,
  withClockGate = true,
  suffix = Option("bpu_abtb")
))
```

### TakenCounter (register, AheadBtb top-level)

```scala
// Source: abtb/AheadBtb.scala:57-63
private val takenCounter = RegInit(
  VecInit.fill(NumBanks)(
    VecInit.fill(NumSets)(
      VecInit.fill(NumWays)(TakenCounter.Zero)
    )
  )
)
```

| Memory         | Depth                      | Width (bit)              | Banks | Read Ports         | Write Ports |
|----------------|----------------------------|--------------------------|-------|--------------------|-------------|
| SRAM (entry) | 32 sets × 8 ways = 256 | AheadBtbEntry size | 4 | 1/bank (single-port, read priority) | 1/bank (queue to write buffer, size=4) |
| takenCounter | 4 banks × 32 sets × 8 ways | TakenCounterWidth = 2-bit | 4 (Reg array dimension) | Full parallel read | conditional update (at t1) |

- NumEntries = 1024, NumBanks = 4, NumWays = 8, NumSets = 1024/8/4 = **32**
- SRAM single-port → If read and write occur at the same time, queue in write buffer (size=4), read takes priority.

---

## 1.3 BTB memory entry description

### AheadBtbEntry (SRAM entry)

```scala
// Source: abtb/Bundles.scala:83-91
class AheadBtbEntry(implicit p: Parameters) extends AheadBtbBundle {
  val valid:           Bool            = Bool()
  val tag:             UInt            = UInt(TagWidth.W)            // 24-bit
  val position:        UInt            = UInt(CfiPositionWidth.W)    // 5-bit
  val attribute:       BranchAttribute = new BranchAttribute         // 4-bit
  val targetLowerBits: UInt            = UInt(TargetLowerBitsWidth.W)// 22-bit
  val targetCarry:     Option[TargetCarry] = if (EnableTargetFix) Option(new TargetCarry) else None // 2-bit (opt)
}
```

| Field | Width (bit) | Source | Description |
|-------|-------------|--------|-------------|
| `valid` | 1 | Write path | Entry occupancy flag. `0` on reset or invalidation (multi-hit eviction). |
| `tag` | 24 (`TagWidth`) | `getTag(PC_B)` at train | PC bits [24:1]. Overlaps `bankIdx`/`setIdx` range intentionally — cross-bank/set collision prevention. Compared against `getTag(s2_startPc)` at predict time. |
| `position` | 5 (`CfiPositionWidth` = `log2Ceil(FetchBlockSize/instBytes)`) | `finalPrediction.cfiPosition` at train | Instruction slot index of the branch within the fetch block. 2-byte aligned (`(branchPc - blockStart) >> 1`). Used to select the earliest taken branch among way hits. |
| `attribute` | 4 (`BranchAttribute`) | PreDecode at train | Branch type + RAS action. See sub-table below. |
| `targetLowerBits` | 22 (`TargetLowerBitsWidth`) | `getTargetLowerBits(target)` at train | PC bits [22:1] of the branch target. Upper bits are recovered from `s2_startPc` + carry at predict time via `getFullTarget()`. |
| `targetCarry` | 2 (`TargetCarry`, optional) | Computed at train | Overflow/underflow flag for target reconstruction when target crosses a `2^(TargetLowerBitsWidth+1)` boundary. **Disabled by default** (`EnableTargetFix = false`). |

**Total entry width (default):** 1 + 24 + 5 + 4 + 22 = **56 bits**

#### BranchAttribute sub-fields

```scala
// Source: bpu/Bundles.scala:37-60
class BranchAttribute extends Bundle {
  val branchType: UInt = BranchAttribute.BranchType()  // 2-bit
  val rasAction:  UInt = BranchAttribute.RasAction()   // 2-bit
}
```

**`branchType` (2-bit)**

| Value | Name | RISC-V instructions | Predict behavior |
|-------|------|---------------------|-----------------|
| `0` | `None` | (no branch) | — |
| `1` | `Conditional` | beq/bne/blt/bge/bltu/bgeu | Direction decided by `takenCounter` (or uTAGE override). Always allocated in ABTB only when taken. |
| `2` | `Direct` | jal, c.jal | Always taken. Target from `targetLowerBits` (static). |
| `3` | `Indirect` | jalr, c.jalr, c.jr | Always taken. Target from `targetLowerBits` (updated on mismatch). RAS provides return target if `isReturn`. |

**`rasAction` (2-bit: `[push, pop]`)**

| Value | Name | Meaning | Used by |
|-------|------|---------|---------|
| `0b00` | `None` | No RAS operation | OtherDirect / OtherIndirect / Conditional |
| `0b01` | `Pop` | Return — pop RAS | `isReturn`: target overridden by uRAS at BPU top |
| `0b10` | `Push` | Call — push return addr | `isCall`: push to RAS speculatively |
| `0b11` | `PopAndPush` | Return-then-call | pop ret addr, push next PC |

**Composite attribute presets (from `BranchAttribute` companion object):**

| Preset | branchType | rasAction | Example |
|--------|-----------|-----------|---------|
| `Conditional` | `1` | `0b00` | beq |
| `OtherDirect` | `2` | `0b00` | j (jal x0) |
| `DirectCall` | `2` | `0b10` | jal x1 |
| `OtherIndirect` | `3` | `0b00` | jr |
| `Return` | `3` | `0b01` | ret (jalr x0, x1) |
| `IndirectCall` | `3` | `0b10` | jalr x1 |
| `ReturnAndCall` | `3` | `0b11` | jalr x1, x1 |

#### TargetCarry (optional, disabled by default)

```scala
// Source: bpu/Bundles.scala:309-329
class TargetCarry extends Bundle {
  val value: UInt = TargetCarry.Value()  // 2-bit enum
  // Fit=0, Overflow=1, Underflow=2
}
```

| Value | Meaning |
|-------|---------|
| `Fit` (0) | `target[22:1]` fits within same upper-bit window as `startPc` |
| `Overflow` (1) | Target crosses boundary upward (upper bits +1) |
| `Underflow` (2) | Target crosses boundary downward (upper bits -1) |

`getFullTarget()` uses this carry to correctly reconstruct the full target address. Without `EnableTargetFix`, upper bits are always taken from `s2_startPc`, causing misprediction for branches near `2^23`-aligned boundaries.

#### TakenCounter (register, separate from SRAM)

```scala
// Source: abtb/Bundles.scala:28-31
object TakenCounter extends SaturateCounterFactory {
  def width(implicit p: Parameters): Int = abtbParameters.TakenCounterWidth  // default: 2
}

// Source: abtb/AheadBtb.scala:57-63
private val takenCounter = RegInit(
  VecInit.fill(NumBanks)(VecInit.fill(NumSets)(VecInit.fill(NumWays)(TakenCounter.Zero)))
)
// Shape: [4 banks][32 sets][8 ways], each 2-bit saturating counter
```

| Value range | Interpretation | State name |
|------------|----------------|------------|
| `0b00` (0) | Strongly not-taken | `SaturateNegative` |
| `0b01` (1) | Weakly not-taken | `WeakNegative` |
| `0b10` (2) | Weakly taken | `WeakPositive` |
| `0b11` (3) | Strongly taken | `SaturatePositive` |

- `isPositive` = `value[1]` = taken prediction output
- New entry allocation → `resetWeakPositive()` (init to `0b10`, weakly taken)
- Train: matching way → `selfIncrease()`, earlier conditional ways → `selfDecrease()`
- Stored entirely in **flip-flops** (not SRAM) for single-cycle read access in s2

### Tag structure (the core of lookahead-read)

```scala
// Source: abtb/Helpers.scala:24-35
val addrFields = AddrField(
  Seq(
("instOffset", instOffsetBits), // PC low-order bits (half word offset within block)
("bankIdx", BankIdxWidth), // → SRAM read index (using s0_previousStartPc)
("setIdx", SetIdxWidth) // → SRAM read index (using s0_previousStartPc)
  ),
  extraFields = Seq(
("tag", instOffsetBits, TagWidth), // → tag comparison (using s1_startPc)
    ("targetLower", instOffsetBits, TargetLowerBitsWidth)
  )
)
// tag starts from instOffsetBits → includes range overlapping with bankIdx/setIdx fields
// In other words, a structure that reads from the bank/set of the previous PC and compares it with the tag of the current PC.
```

---

## 1.4 Pair prediction unit

```scala
// Source: bpu/Bpu.scala:266-278
private val s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction
// abtb.io.prediction: Vec(NumWays=8, Valid[Prediction])
private val s1_utageHitMask = VecInit(s1_btbPrediction.map { pred =>
  pred.valid && utage.io.prediction.valid &&
    utage.io.prediction.bits.cfiPosition === pred.bits.cfiPosition
})
private val s1_takenMask = VecInit(s1_btbPrediction.zipWithIndex.map { case (pred, i) =>
  pred.valid && (
    pred.bits.attribute.isDirect || pred.bits.attribute.isIndirect ||
    pred.bits.attribute.isConditional && Mux(s1_utageHitMask(i), utage.io.prediction.bits.taken, pred.bits.taken)
  )
})
```

| BTB | Paired Unit | role | Combination method |
|------|-------------------|------------------------------------|-----------|
| abtb | MicroTage (utage) | Conditional branch direction determination (override) | When matching abtb entry hit + position, use utage.taken |
| abtb | MicroRas (uras) | return address provided | If s1_prediction.attribute.isReturn, use uras.retTarget |

- Arbiter input: uBTB hit result (1 slot) + ABTB tag-matched way results (0~N slots); winner = smallest `cfiPosition` among valid taken entries
- Final decision on direction of conditional branch: utage.taken or abtb.taken depending on whether utage hit or not

### uBTB vs ABTB simultaneous hit arbitration

```scala
// Source: bpu/Bpu.scala:266-304
private val s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction
// abtb.io.prediction: Vec(8, Valid[Prediction])
//   → each way's .valid = s2_hitMask[i] (tag match result)
//   → non-matching ways have valid=false and are excluded from selection
```

ABTB 8-way entries are already filtered by tag match before entering the arbiter — only tag-hit ways have `valid=true`. The selection pool is therefore:

- **1 slot**: uBTB hit result (valid if uBTB hits)
- **0~N slots**: ABTB tag-matched way results (valid only for ways where `entry.tag === getTag(PC_B)`)

There is **no fixed predictor priority**. Among all valid taken entries, the one with the smallest `cfiPosition` (earliest branch in fetch block) is selected:

```scala
// Source: bpu/Bpu.scala:298-304
private val s1_compareMatrix      = CompareMatrix(VecInit(s1_btbPrediction.map(_.bits.cfiPosition)))
private val s1_firstTakenBranchOH = s1_compareMatrix.getLeastElementOH(s1_takenMask)
private val s1_firstTakenBranch   = Mux1H(s1_firstTakenBranchOH, s1_btbPrediction)
```

| Scenario | Winner |
|----------|--------|
| Only uBTB hits | uBTB |
| Only ABTB hits | ABTB tag-matched way(s), smallest `cfiPosition` |
| Both hit, **different** `cfiPosition` | Whichever has the **smaller** `cfiPosition` (earlier in fetch block) |
| Both hit, **same** `cfiPosition` | Same branch — results consistent |

```scala
// Debug signals (bpu/Bpu.scala:311-314)
// s1_firstTakenBranchOH(0) = uBTB slot (index 0 in s1_btbPrediction)
debug_s1UseUbtb      = s1_taken && s1_firstTakenBranchOH(0) && !s1_utageHitMask(0)
debug_s1UseUbtbUtage = s1_taken && s1_firstTakenBranchOH(0) && s1_utageHitMask(0)
debug_s1UseAbtb      = s1_taken && !s1_firstTakenBranchOH(0) && !s1_utageHitMask.drop(1).reduce(_ || _)
debug_s1UseAbtbUtage = s1_taken && !s1_firstTakenBranchOH(0) && s1_utageHitMask.drop(1).reduce(_ || _)
```

---

## 1.5 Next prediction pseudocode

```text
onPredict(PC_A, PC_B):
  // --- abtb s0 ---
// Read “first” with PC_A.
  bank.readReq(setIdx = getSetIndex(PC_A),
               bankMask = UIntToOH(getBankIndex(PC_A)))

  // --- abtb s1 ---
// The previous cycle read response is a PC_A-based row.
// At the same time, latch the current prediction target PC_B for s2.
  s1_startPc = io.startPc   // = PC_B
  entries = bank.readResp() // entries@PC_A

  // --- abtb s2 ---
  s2_startPc = RegEnable(s1_startPc, s1_fire)   // = PC_B
  s2_entries = RegEnable(entries, s1_fire)
  s2_tag = getTag(s2_startPc)                   // getTag(PC_B)
  s2_hitMask[i] = s2_entries[i].valid && s2_entries[i].tag === s2_tag

// Detect multiple hits (same position) → Invalidate one when multiple hits occur
  if detectMultiHit(s2_hitMask, s2_entries.map(_.position)):
    bank.writeInvalidate(multiHitWayIdx)

// Determine direction with taken counter
  s2_ctrResult[i] = takenCounter[bankIdx][setIdx][i].isPositive

  for i in 0..NumWays-1:
    prediction[i].valid       = s2_valid && s2_hitMask[i]
    prediction[i].taken       = s2_ctrResult[i]
    prediction[i].cfiPosition = s2_entries[i].position
    prediction[i].attribute   = s2_entries[i].attribute
    prediction[i].target      = getFullTarget(s2_startPc, s2_entries[i].targetLowerBits)

// Can override utage direction from BPU top (limited to conditional branch)
  for i in 0..8 (ubtb+abtb):
    if utage.hit && position match:
      s1_takenMask[i].taken = utage.taken
```

---

## 1.6 Input-to-output latency and throughput

| BTB  | Input Stage  | Output Stage        | Latency (cycle) | Throughput (pred/cycle) |
|------|--------------|---------------------|-----------------|--------------------------|
| abtb | abtb s0 (BPU s0) | abtb s2 (BPU s1 output) | 2 (inside s0→s1→s2) | 1 |

- Internal pipeline: s0 (SRAM read req) → s1 (entries arrival, s1_startPc latch) → s2 (tag comparison, output)
- From a BPU perspective, the prediction result is valid at s1 (s2_valid = true after s1_fire)
- predictionSent = io.stageCtrl.s1_fire (BPU s1_fire) → abtb s2_fire trigger

```scala
// Source: abtb/AheadBtb.scala:78-99
s0_fire := io.enable && predictReqValid          // predictReqValid = io.stageCtrl.s0_fire
s1_fire := io.enable && s1_valid && s2_ready && predictReqValid
s2_fire := io.enable && s2_valid && predictionSent  // predictionSent = io.stageCtrl.s1_fire
```

---

## 1.7 Pipeline stage location

| Signal                 | Produced @ Stage         | Consumed @ Stage   | Timing Note |
|------------------------|--------------------------|--------------------|-------------|
| s0_previousStartPc | BPU s0 (io.startPc) | abtb s0 | Directly connected to bank.readReq |
| bank.readResp.entries  | abtb s1 (SRAM latency 1) | abtb s1            | s1_entries = Mux1H(bankMask, ...) |
| s1_startPc | abtb s1 (new io.startPc) | abtb s2 | `RegEnable(s1_startPc, s1_fire)` |
| s2_entries             | abtb s2                  | abtb s2            | `RegEnable(entries, s1_fire)` |
| io.prediction[0..7]    | abtb s2                  | BPU s1             | s2_valid = true |
| io.meta | abtb s2 | BPU s2→s3 (for fastTrain) | `s2_abtbMeta = RegEnable(abtb.io.meta, s1_fire)` |

```scala
// Source: abtb/AheadBtb.scala:143-145, 148-152
private val s2_setIdx   = RegEnable(Mux(overrideValid, s3_setIdx, s1_setIdx), s1_fire)
private val s2_entries  = RegEnable(Mux(overrideValid, s3_entries, s1_entries), s1_fire)
private val s2_startPc  = RegEnable(s1_startPc, s1_fire)

// When overrideValid, re-predict one cycle faster by reusing the s3 (previous s2) value
s2_ready := s2_fire || !s2_valid || overrideValid || redirectValid
```

- **When Override occurs**: Supports immediate re-prediction by resupplying the s3 register (previous s2 result) to s2.

---

## 1.8 BTB memory indexing hashing method

### AddrField Layout

```scala
// Source: abtb/Helpers.scala:24-35
val addrFields = AddrField(
  Seq(
    ("instOffset", instOffsetBits),  // PC bit [0:0]
    ("bankIdx",    BankIdxWidth),    // PC bit [2:1]  (BankIdxWidth = log2Ceil(4) = 2)
    ("setIdx",     SetIdxWidth)      // PC bit [7:3]  (SetIdxWidth  = log2Ceil(32) = 5)
  ),
  maxWidth = Option(VAddrBits),
  extraFields = Seq(
    ("tag",         instOffsetBits, TagWidth),              // PC bit [24:1]  (TagWidth=24)
    ("targetLower", instOffsetBits, TargetLowerBitsWidth)   // PC bit [22:1]
  )
)
```

### SRAM physics

It consists of 4 independent banks, and each bank has 32 sets × 8 ways SRAM.

```
AheadBtb
├── bank[0]: SRAM (32 sets × 8 ways)  ← AheadBtbBank
├── bank[1]: SRAM (32 sets × 8 ways)
├── bank[2]: SRAM (32 sets × 8 ways)
└── bank[3]: SRAM (32 sets × 8 ways)
```

### Predict path: PC_A → bankIdx/setIdx, PC_B → tag

Key characteristics of abtb: SRAM index is PC_A (s0_previousStartPc), tag comparison is PC_B (s2_startPc).

```scala
// Source: abtb/Helpers.scala:37-44
def getSetIndex(pc: PrunedAddr): UInt = addrFields.extract("setIdx", pc)
def getBankIndex(pc: PrunedAddr): UInt = addrFields.extract("bankIdx", pc)
def getTag(pc: PrunedAddr): UInt       = addrFields.extract("tag",     pc)
```

| Path | Field | PC Source | PC Bits | Width | History |
|------|-------|---------|---------|-------|---------|
| Predict SRAM read | bankIdx | PC_A (s0_previousStartPc) | [2:1] | 2 | None |
| Predict SRAM read | setIdx | PC_A (s0_previousStartPc) | [7:3] | 5 | None |
| Predict tag compare | tag | PC_B (s2_startPc) | [24:1] | 24 | None |

### Bank → Set → Way access sequence

```scala
// Source: abtb/AheadBtb.scala:110-130
// s0: Read req only for the bank with bankIdx, specify row with setIdx
val s0_bankMask = UIntToOH(s0_bankIdx)
banks.zipWithIndex.foreach { case (b, i) =>
b.io.readReq.valid := predictReqValid && s0_bankMask(i) // Only 1 bank is active
b.io.readReq.bits.setIdx := s0_setIdx // 1 row out of 32 sets
}

// s1: Receive all 8 ways from the selected bank's response
val s1_entries = Mux1H(s1_bankMask, banks.map(_.io.readResp.entries))  // Vec(8, AheadBtbEntry)

// s2: 8 ways each tag comparison → hit mask (tag compare, not address select)
s2_hitMask[i] = s2_entries[i].valid && s2_entries[i].tag === s2_tag
prediction[i].valid = s2_valid && s2_hitMask[i]
```

| steps | Action | PC Source | Results |
|------|------|---------|------|
| s0: bank select | `bankIdx`=PC[2:1] → `UIntToOH` → readReq to only 1 bank | PC_A | 1 of 4 banks activated |
| s0: set address | `setIdx`=PC[7:3] → SRAM row address | PC_A | Designate 1 row out of 32 sets |
| s1: row read | Selected bank → `Mux1H` → Return all 8 ways | — | Vec(8, AheadBtbEntry) |
| s2: way determination | **tag compare** (not address select) `entry[i].tag === getTag(PC_B)` | PC_B | hitMask[0..7] |

- Way selection is not “select one using the address bit”, but reads all 8 ways and determines whether it is a hit using tag match**.
- Multiple hits (different ways, same position) may occur, and in case of multi-hit, one is invalidated.
- Among the ways hit on the BPU top, `position` takes the leading branch and finally selects it.

**tag overlap design**: The `tag` extraField starts from `instOffsetBits=1` and is 24-bit wide, so it includes the `bankIdx[2:1]` and `setIdx[7:3]` bit ranges.
This is by intentional design — the tag includes the entire bankIdx/setIdx range to prevent cross-bank·cross-set misidentification.

### Train path

```scala
// Source: abtb/AheadBtb.scala (train implementation part)
// When t1 write: setIdx, bankMask → Restore from abtbMeta (value saved at prediction time)
// tag → getTag(t1_train.startPc) directly extracted
```

| Path | Field | Source | Note |
|------|-------|------|------|
| Train (t1) | setIdx | abtbMeta.setIdx | Reusing the value saved at predict time |
| Train (t1) | bankMask | abtbMeta.bankMask | Reusing the value saved at predict time |
| Train (t1) | tag | getTag(t1_train.startPc) | Extract directly from your PC |

- Use history: **None**
- No hash (XOR/fold not applied), PC simple bit extraction

---

## 1.9 Training method

### Trigger: fast-train (s3 finalPrediction + abtbMeta)

```scala
// Source: abtb/AheadBtb.scala:204-215
private val t0_train = io.fastTrain.get.bits
private val t0_fire  = io.enable && io.fastTrain.get.valid &&
                       t0_train.finalPrediction.taken &&
                       t0_train.abtbMeta.valid
// → Condition: train only if s3 finalPrediction is taken and abtbMeta is valid
```

```scala
// Source: bpu/Bpu.scala:182-188
fastTrain.bits.abtbMeta := s3_abtbMeta // Value latched from s2_abtbMeta to s3
// AheadBtbMeta: valid, setIdx, bankMask, entries[NumWays](hit, attribute, position, targetLowerBits)
```

### BPU internal meta delivery (AheadBtbMeta)

```scala
// Source: abtb/Bundles.scala:69-81
class AheadBtbMetaEntry(implicit p: Parameters) extends AheadBtbBundle {
  val hit:             Bool            = Bool()
  val attribute:       BranchAttribute = new BranchAttribute
  val position:        UInt            = UInt(CfiPositionWidth.W)
  val targetLowerBits: UInt            = UInt(TargetLowerBitsWidth.W)
}
class AheadBtbMeta(implicit p: Parameters) extends AheadBtbBundle {
  val valid:    Bool                   = Bool()
  val setIdx:   UInt                   = UInt(SetIdxWidth.W)
  val bankMask: UInt                   = UInt(NumBanks.W)
  val entries:  Vec[AheadBtbMetaEntry] = Vec(NumWays, new AheadBtbMetaEntry())
}
```

### t1 train operation

```scala
// Source: abtb/AheadBtb.scala:230-303 (simplified)
// update taken counter
for each way:
  if cond && posBefore: decrease (branch before taken branch)
  if cond && posEqual:  increase (matching branch position)
if writeResp.needResetCtr: resetWeakPositive (when assigning a new entry)

// update entry
if not hit (based on position+attribute):
  write new entry to victim way (PLRU)
elif indirect && target mismatch:
  correct target in existing entry
```

| Trigger                                | Required FastTrain Info                    | Storage Path               | Write Port / Conflict Handling |
|----------------------------------------|--------------------------------------------|----------------------------|--------------------------------|
| s3_valid && finalPrediction.taken && abtbMeta.valid | startPc, finalPrediction (taken, position, target, attribute), abtbMeta (setIdx, bankMask, per-way hit/attr/pos/target) | `s2_abtbMeta -> s3_abtbMeta -> fastTrain` (no FTQ storage) | SRAM single-port: read first, write queues write buffer (size=4) |

- AheadBtbMeta is only transferred to BPU internal registers (`s2_abtbMeta`, `s3_abtbMeta`) (not stored in FTQ)
- When the write buffer is full, the write request is dropped (`write_buffer_full_drop_write` perf counter)

---

## 1.10 Override and redirection

```scala
// Source: abtb/AheadBtb.scala:80-99 (abtb internal flush/ready logic)
s2_flush := redirectValid            // backend redirect → abtb s2 flush
s1_flush := s2_flush
s2_ready := s2_fire || !s2_valid || overrideValid || redirectValid
// overrideValid = s3_override (injected from BPU top)
// → When s3_override occurs, s2 is immediately freed and can be re-predicted with the s3 value in the next cycle.

// Source: bpu/Bpu.scala:200-201
abtb.io.redirectValid := redirect.valid
abtb.io.overrideValid := s3_override
```

```scala
// Source: bpu/Bpu.scala:434-441 (Select BPU top next PC)
s0_startPc := MuxCase(
  s0_startPcReg,
  Seq(
redirect.valid -> redirect.bits.target, // priority: backend redirect
s3_override -> s3_prediction.target, // 2nd priority: mbtb s3 override
s1_valid -> s1_prediction.target // 3rd priority: abtb/ubtb s1 prediction
  )
)
```

| Condition               | Winner            | Redirect Target          | Side Effect (Flush/Replay) |
|-------------------------|-------------------|--------------------------|----------------------------|
| redirect.valid | Backend | redirect.bits.target | abtb s2_flush, s1_flush; BPU entire pipe flush |
| s3_override | mbtb s3 results | s3_prediction.target | abtb s2_ready immediately free; Reforecast next cycle with s3 value |
| !redirect && !s3_override && s1_taken | first taken(min position) among uBTB+ABTB candidates | s1_prediction.target | None |
| !redirect && !s3_override && !s1_taken | FallThrough | s1_prediction.target | None |

- Selection of `s1_taken` and `first taken(min position)` is determined by BPU top (`s1_takenMask`, `CompareMatrix`, `s1_firstTakenBranch`)
  (`bpu/Bpu.scala:266-304`)
- When s3_override: `s2_entries/s2_startPc` in abtb is replaced by Mux with s3 value (`s3_entries/s3_setIdx/...`)
→ mbtb-based re-prediction possible in 1 cycle without separate SRAM read

---

## Quality Checklist

- [x] Comply with order 1.1~1.10
- [x] specify memory depth/width/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] Specify pair predictor combination rules (utage, uras)
- Includes [x] pseudocode
- [x] latency/throughput quantification (2 cycle internal, BPU s1 output)
- [x] stage input/output timing specified
- [x] indexing/hash expression + PC/history bit position specified
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
- [x] Override/redirection priority and rationale specified
