# ubtb (MicroBtb) analysis

> Analysis criteria: BTB_analysis_rule.md
> Analysis target: `src/main/scala/xiangshan/frontend/bpu/ubtb/`
> Code-based only — No use of prior knowledge/web-search

---

## 1.1 BTB types and roles

MicroBtb is **Block-type BTB**, which predicts the first branch taken from the current input fetch block.
It is a fully-associative structure, and when hit, it is always processed as taken(`io.prediction.bits.taken := s1_hit`).
The prediction distance is **the "next block"** of the current block (not lookahead).

| BTB | Type | Block Width | Predict Distance | Description |
|------|--------|----------------------|---------------------------------|------|
| ubtb | Block | FetchBlockSize (bytes) | Current block → next block (s1 output) | 32-entry fully-associative, always taken predictions |

```
// Source: ubtb/MicroBtb.scala:78-83
// we do always-taken prediction in ubtb
io.prediction.valid            := s1_hit
io.prediction.bits.taken       := s1_hit
io.prediction.bits.cfiPosition := s1_hitEntry.slot1.position
io.prediction.bits.target      := getFullTarget(s1_startPc, s1_hitEntry.slot1.target, s1_hitEntry.slot1.targetCarry)
io.prediction.bits.attribute   := s1_hitEntry.slot1.attribute
```

---

## 1.2 BTB memory spec

MicroBtb is implemented as a register array, not **SRAM**.

```scala
// Source: ubtb/MicroBtb.scala:49
private val entries = RegInit(VecInit(Seq.fill(NumEntries)(0.U.asTypeOf(new MicroBtbEntry))))
```

| Memory    | Depth       | Width (bit)                         | Banks | Read Ports | Write Ports |
|-----------|-------------|-------------------------------------|-------|------------|-------------|
| entries | 32 (NumEntries) | tag(22) + usefulCnt(2) + slot1 + slot2 | 1 (full-assoc) | 32 (parallel full read) | 1 (selected entry write-back) |

- slot1 width: position(CfiPositionWidth) + attribute(4) + target(22) + isStaticTarget(1)
- slot2 width: valid(1) + position(CfiPositionWidth) + attribute(4) + target(22) + taken(1)
- BranchAttribute: branchType(2-bit EnumUInt(4)) + rasAction(2-bit EnumUInt(4)) = 4 bit
- CfiPositionWidth = FetchBlockSizeWidth = log2Ceil(FetchBlockSize) [parameter dependent]

---

## 1.3 BTB memory entry description

```scala
// Source: ubtb/Bundles.scala:32-66
class MicroBtbEntry(implicit p: Parameters) extends MicroBtbBundle {
  class SlotBase extends Bundle {
val position: UInt = UInt(CfiPositionWidth.W) // branch position in fetch block
    val attribute: BranchAttribute = new BranchAttribute
    val target: UInt    = UInt(TargetWidth.W)           // 22-bit partial target
    val targetCarry: Option[TargetCarry] = if (EnableTargetFix) Option(new TargetCarry) else None
  }
  class Slot1 extends SlotBase {
val isStaticTarget: Bool = Bool() // Whether it always moves to the same target
  }
  class Slot2 extends SlotBase {
val valid: Bool = Bool() // Is slot2 valid?
val taken: Bool = Bool() // slot2 branch prediction direction
  }
  def valid: Bool = !usefulCnt.isSaturateNegative  // usefulCnt > min → valid
  val tag: UInt       = UInt(TagWidth.W)            // 22-bit partial vTag
val usefulCnt: SaturateCounter = UsefulCounter() // 2-bit saturation counter (determines whether it is useful)
  val slot1: Slot1 = new Slot1
  val slot2: Slot2 = new Slot2
}
```

| Field Name            | Width (bit)         | Description |
|-----------------------|---------------------|-------------|
| tag | 22 (TagWidth) | PC[instOffsetBits + TagWidth - 1 : instOffsetBits], for tag comparison |
| usefulCnt | 2 (UsefulCntWidth) | saturation counter; min = invalid entry |
| slot1.position | CfiPositionWidth | First branch location in fetch block |
| slot1.attribute | 4 | BranchAttribute (branchType 2-bit + rasAction 2-bit) — See below for details |
| slot1.target | 22 (TargetWidth) | target low bit; parent derives from startPc |
| slot1.isStaticTarget | 1 | true = When only the same target is seen (dynamic target tracking) |
| slot1.targetCarry | 2 (opt) | EnableTargetFix=false default → not included |
| slot2.valid | 1 | Whether to use slot2 (current TODO status) |
| slot2.position | CfiPositionWidth | Second branch location |
| slot2.attribute | 4 | second branch attribute |
| slot2.target | 22 | Second branch target child |
| slot2.taken | 1 | Second branch direction |

### BranchAttribute encoding (4-bit)

```scala
// Source: bpu/Bundles.scala:37-140
class BranchAttribute extends Bundle {
  val branchType: UInt = BranchAttribute.BranchType()  // bits[1:0]
  val rasAction:  UInt = BranchAttribute.RasAction()   // bits[3:2]
}

object BranchAttribute {
  object BranchType extends EnumUInt(4) {  // width = ceil(log2(4)) = 2 bit
def None: UInt = 0.U // 0b00 — not branch (fallthrough)
    def Conditional: UInt = 1.U  // 0b01 — beq/bne/blt/bge/bltu/bgeu
def Direct: UInt = 2.U // 0b10 — j/jal (fixed offset)
def Indirect: UInt = 3.U // 0b11 — jr/jalr (register-based)
  }
  object RasAction extends EnumUInt(4) {  // width = 2 bit
    def popBit  = 0  // bit[0]: pop (return)
    def pushBit = 1  // bit[1]: push (call)
def None: UInt = 0.U // 0b00 — No RAS action
    def Pop:        UInt = 1.U  // 0b01 — return (rs=x1/x5, rd≠rs)
    def Push:       UInt = 2.U  // 0b10 — call  (rd=x1/x5)
    def PopAndPush: UInt = 3.U  // 0b11 — return & call (rs=rd=x1/x5, rs≠rd)
  }
}
```

**bit layout (Chisel Bundle: first declared field is LSB)**

```
bit[3]  bit[2]  bit[1]  bit[0]
  rasAction[1]    rasAction[0]    branchType[1]   branchType[0]
  (push)          (pop)
```

**List of valid combinations (decode results)**

| name | branchType [1:0] | rasAction [3:2] | 4-bit (hex) | Conditions (RISC-V) | needIttage |
|---------------|------------------|-----------------|-------------|---------------------------------------------|------------|
| None | 00 | 00 | 0x0 | not branch | false |
| Conditional   | 01               | 00              | 0x1         | beq/bne/blt/bge/bltu/bgeu                  | false      |
| OtherDirect | 10 | 00 | 0x2 | j/jal (rd≠x1/x5 or RVC j/jr) | false |
| DirectCall    | 10               | 10              | 0xA         | jal with rd=x1/x5                           | false      |
| OtherIndirect | 11               | 00              | 0x3         | jalr, rd≠x1/x5, rs≠x1/x5                  | **true**   |
| Return        | 11               | 01              | 0x7         | jalr, rs=x1/x5, rd≠rs                      | false      |
| IndirectCall | 11 | 10 | 0xB | jalr, rd=x1/x5, rs≠x1/x5 (or rs≠rd) | **true** |
| ReturnAndCall | 11               | 11              | 0xF         | jalr, rd=x1/x5, rs=x1/x5, rs≠rd            | false      |

```scala
// Source: bpu/Bundles.scala:60
def needIttage: Bool = isIndirect && !hasPop // OtherIndirect + IndirectCall only
// hasPop = rasAction(popBit) = rasAction[0]
```

**Summary of attribute usage by predictor**

| judgment method | Conditions | ubtb utilization location |
|--------------------|-----------------------------|----------------|
| `isConditional` | branchType == 01 | override taken or not by utage |
| `isDirect` | branchType == 10 | Always taken as predicted |
| `isIndirect` | branchType == 11 | always taken; If needIttage, replace target from s3 to ITTage |
| `isReturn`/`hasPop`| rasAction[0] == 1 | Replace target with uras.retTarget in s1 |
| `isCall`/`hasPush` | rasAction[1] == 1 | RAS push (processed by ras module) |

---

## 1.4 Pair prediction unit

```scala
// Source: bpu/Bpu.scala:266-277
private val s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction
private val s1_utageHitMask = VecInit(s1_btbPrediction.map { pred =>
  pred.valid && utage.io.prediction.valid && utage.io.prediction.bits.cfiPosition === pred.bits.cfiPosition
})
private val s1_takenMask = VecInit(s1_btbPrediction.zipWithIndex.map { case (pred, i) =>
  val utageHit   = s1_utageHitMask(i)
  val utageTaken = utage.io.prediction.bits.taken
  pred.valid && (
    pred.bits.attribute.isDirect ||
    pred.bits.attribute.isIndirect ||
    pred.bits.attribute.isConditional && Mux(utageHit, utageTaken, pred.bits.taken)
  )
})
```

| BTB | Paired Unit | Role | Combination method |
|------|-------------------|-----------------------------------|-----------|
| ubtb | MicroTage (utage) | Conditional branch direction determination (override) | When matching ubtb hit + position, use utage.taken |
| ubtb | - | Direct/indirect branch target | Use ubtb’s target as is |

- If MicroTage hits (position matches), replace the taken bit of ubtb with the taken of utage.
- MicroRas (uras) can also replace the return address in s1.

```scala
// Source: bpu/Bpu.scala:307-309
private val s1_isRet = s1_prediction.attribute.isReturn
when(s1_isRet && uras.io.specOut.isCanUse) {
  s1_prediction.target := uras.io.specOut.retTarget
}
```

---

## 1.5 Next prediction pseudocode

```text
onPredict(startPc):
  // s0: latch input PC
  s1_startPc = RegEnable(startPc, s0_fire)

  // s1: full-associative tag compare
  s1_tag = getTag(s1_startPc)         // PC[instOffsetBits + TagWidth - 1 : instOffsetBits]
  s1_hitOH = entries.map(e => e.valid && e.tag === s1_tag)  // valid = usefulCnt > min
assert(PopCount(s1_hitOH) <= 1) // max 1-hot

  if s1_hitOH.orR:
    hit = true
    hitEntry = entries(OHToUInt(s1_hitOH))
    output.valid      = true
output.taken = true // always taken
    output.cfiPosition = hitEntry.slot1.position
    output.target     = getFullTarget(s1_startPc, hitEntry.slot1.target)
    output.attribute  = hitEntry.slot1.attribute
  else:
    output.valid = false

// utage can override the conditional branch direction at the BPU top
  if utage.prediction.valid && utage.position == output.cfiPosition:
    output.taken (effective) = utage.taken

  // replacer touch on predict hit
  replacer.predTouch = (s1_hit && s1_fire, s1_hitIdx)
```

---

## 1.6 Input-to-output latency and throughput

| BTB  | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|-------------|--------------|-----------------|--------------------------|
| ubtb | s0 (startPc) | s1 (prediction valid) | 1 | 1 |

- Latch startPc with RegEnable in s0, and output within 1 cycle after 32-entry parallel comparison in s1.
- No SRAM read latency because it is register-based → 1-cycle prediction

---

## 1.7 Pipeline stage location

| Signal               | Produced @ Stage | Consumed @ Stage | Timing Note |
|----------------------|------------------|------------------|-------------|
| s0_startPc | BPU s0 | ubtb s0 | io.startPc direct connection |
| s1_startPc           | ubtb s0 → s1     | ubtb s1          | `RegEnable(s0_startPc, s0_fire)` |
| s1_hitOH / s1_hit | ubtb s1 | ubtb s1 | register entries combinational logic |
| io.prediction        | ubtb s1          | BPU s1           | valid := s1_hit |
| s1_btbPrediction[0]  | BPU s1           | BPU s1           | VecInit(ubtb.io.prediction) ++ abtb.io.prediction |

```scala
// Source: ubtb/MicroBtb.scala:69-76
private val s1_startPc = RegEnable(s0_startPc, s0_fire)
private val s1_tag     = getTag(s1_startPc)
private val s1_hitOH   = VecInit(entries.map(e => e.valid && e.tag === s1_tag)).asUInt
private val s1_hit     = s1_hitOH.orR
private val s1_hitIdx  = OHToUInt(s1_hitOH)
private val s1_hitEntry = entries(s1_hitIdx)
```

---

## 1.8 BTB memory indexing hashing method

### Structure: Fully-Associative, no index

ubtb has a fully associative register-file structure, so there is no setIdx / bankIdx.
The hit is determined by parallel tag comparison of all 32 entries.

```scala
// Source: ubtb/Helpers.scala:25-34
val addrFields = AddrField(
  Seq(
("instOffset", instOffsetBits), // PC bit [0:0], always 0 (2B aligned)
    ("tag", TagWidth)                // PC bit [22:1]  (TagWidth=22)
  ),
  maxWidth = Option(VAddrBits),
  extraFields = Seq(
    ("targetLower", instOffsetBits, TargetWidth)  // target bit [22:1]
  )
)
```

### tag calculation formula

```scala
// Source: ubtb/Helpers.scala:36-37
def getTag(pc: PrunedAddr): UInt =
  addrFields.extract("tag", pc)
// ↑ tag = pc[instOffsetBits + TagWidth - 1 : instOffsetBits]
//       = pc[1 + 22 - 1 : 1] = pc[22:1]
```

- Use history: **None**
- No hash (XOR/fold not applied), PC simple bit extraction

| Path | Field | Formula | PC Bits | History | Note |
|------|-------|---------|---------|---------|------|
| Predict (s1) | tag | pc[22:1] | [22:1] | None | s1_startPc |
| Train (t0) | tag | pc[22:1] | [22:1] | None | fastTrain.startPc |

> Use the same formula for both Predict path and Train path.
> instOffset (bit [0]) is always 0 for 2B sorting — not used for sorting.

---

## 1.9 Training method

### fast-train trigger condition

```scala
// Source: ubtb/MicroBtb.scala:102-117
if (UseFastTrain) {
t0_fire := io.fastTrain.get.valid && io.enable // ← Condition key
  t0_startPc     := io.fastTrain.get.bits.startPc
  t0_actualTaken := io.fastTrain.get.bits.finalPrediction.taken
  t0_position    := io.fastTrain.get.bits.finalPrediction.cfiPosition
  t0_fullTarget  := io.fastTrain.get.bits.finalPrediction.target
  t0_attribute   := io.fastTrain.get.bits.finalPrediction.attribute
} else {
// slow mode: train only on FTQ commits with mispredict
  t0_fire        := io.stageCtrl.t0_fire && io.train.mispredictBranch.valid && io.enable
  ...
}
```

```scala
// Source: bpu/Bpu.scala:182-188
private val fastTrain = Wire(Valid(new BpuFastTrain))
fastTrain.valid := s3_valid // ← Every cycle the BPU s3 pipeline is valid
fastTrain.bits.startPc         := s3_startPc
fastTrain.bits.finalPrediction := s3_prediction // mbtb+Tage+Sc+ITTage+RAS final combined result
fastTrain.bits.abtbMeta        := s3_abtbMeta
fastTrain.bits.utageMeta       := s3_utageMeta
fastTrain.bits.hasOverride     := s3_override
```

**Condition for fast-train to fire: `s3_valid == true` (BPU s3 pipeline valid)**

In other words, ubtb trains every cycle when BPU s3 is valid, regardless of **taken/not-taken or mispredict**.
Since it is `t0_actualTaken = finalPrediction.taken`, the actual action taken depends on whether or not it was taken:

| Conditions | t0_fire | t0_actualTaken | Action at t1 |
|-----------------------------|---------|----------------|---------------|
| s3_valid && prediction.taken | true | true | hit → usefulCnt increase/decrease / miss → allocate |
| s3_valid && !prediction.taken | true | false | hit → usefulCnt decrease or re-init / miss → **do nothing** (allocate condition not met) |
| !s3_valid | false | — | no train |

> **Compare slow mode**: `io.stageCtrl.t0_fire && mispredictBranch.valid` — train only commits with mispredicts.
> There is a FIXME annotation (`// FIXME: not sure if first mispredict is the best, maybe first taken?`), so the design has not yet been confirmed.

---

### Train pipeline: t0 → t1

```scala
// Source: ubtb/MicroBtb.scala:162-228
t1_fire := RegNext(t0_fire, false.B) // 1-cycle delay of t0

// Action determined at t1 (3 cases)
when(t1_fire) {
  when(!t1_hit) {
// case 1: Assign new entry only if miss → taken (initEntryIfNotUseful(true.B))
    initEntryIfNotUseful(true.B)
  }.elsewhen(!t1_hitAttributeSame || !t1_hitPositionSame || !t1_hitTargetSame || !t1_actualTaken) {
// case 2: hit + (position/attribute/target mismatch OR not-taken)
// - If notUseful, reinitialize with a new entry.
// - If useful, decrease usefulCnt
// - If target mismatch, isStaticTarget := false
    initEntryIfNotUseful(t1_hitNotUseful)
    when(!t1_hitTargetSame) { t1_updatedEntry.slot1.isStaticTarget := false.B }
  }.otherwise {
// case 3: hit + match all fields + taken → increase usefulCnt
    t1_updatedEntry.usefulCnt := t1_hitEntry.usefulCnt.getIncrease()
  }
}

// write-back: reflected in entries only in case of hit or allocate
t1_allocate := !t1_hit && t1_actualTaken // New allocation only when miss + taken
t1_updateIdx := Mux(t1_hit, t1_hitIdx, replacer.io.victim)
when(t1_fire && (t1_hit || t1_allocate)) {
  entries(t1_updateIdx) := t1_updatedEntry
}
```

**usefulCnt** when initializing entry: `resetSaturatePositive()` (starts with maximum value, then increases if correct/decrease if incorrect).

---

### Victim entry decision (MicroBtbReplacer)

```scala
// Source: ubtb/MicroBtbReplacer.scala:38-52
private val replacer = ReplacementPolicy.fromString(Replacer, NumEntries)  // PLRU (NumEntries=32)

// Step 1: Search for the entry where usefulCnt is the minimum value (isSaturateNegative)
private val notUsefulVec = VecInit(io.usefulCnt.map(_.isSaturateNegative))
private val notUseful    = notUsefulVec.reduce(_ || _)
private val notUsefulIdx = PriorityEncoder(notUsefulVec) // first not-useful from index 0

// Step 2: If there is not-useful, use it first. If not, use PLRU victim.
io.victim := Mux(notUseful, notUsefulIdx, replacer.way)

// PLRU status update: reflects both predict hit and train touch
replacer.access(Seq(io.predTouch, io.trainTouch))
// predTouch: valid = s1_hit && s1_fire (hit entry touch when predicting)
// trainTouch: valid = t1_fire, bits = t1_updateIdx (entry touch updated during train)
```

**victim selection priority summary**

```
priority 1 (priority): lowest index among notUseful entries (PriorityEncoder)
priority 2 (fallback): way pointed out by PLRU replacer
```

| Situation | victim source | Remarks |
|------------------------------------|-----------------|------|
| At least one usefulCnt has a minimum | PriorityEncoder | Scan from index 0, select first not-useful |
| All entries are useful | PLRU .way | Based on PLRU tree updated with predict touch + train touch |

> The victim is actually used only when the `t1_allocate (= !t1_hit && t1_actualTaken)` condition is true.
> In case of a hit, ignore the victim and write-back directly to `t1_hitIdx`.

---

### Continuous train collision handling (t0-t1 hazard)

Data hazard may occur when t0 and t1 are triggered consecutively:

```scala
// Source: ubtb/MicroBtb.scala:128-148
// Problem 1: The entry being written by t1 is read from entries[] that t0 has not yet reflected, misjudged as a miss → incorrect allocate
// Problem 2: t1 selects t0's hit entry as victim and is replacing it → t0 must pretend to be a miss
private val t0_hitT1Update = Wire(Bool())
private val t0_hitT1Victim = t1_fire && t0_realHitIdx === replacer.io.victim && t1_allocate

// Final hit judgment correction
private val t0_hit = t0_realHit && !t0_hitT1Victim || t0_hitT1Update

// t0_hitT1Update: If t1 is updating the same tag, t0 treats t1's updatedEntry as "previewed"
t0_hitT1Update := t1_fire && t0_tag === t1_tag && (t1_hit || t1_allocate)
// → t0_hitEntry = t1_updatedEntry (wire forwarding)
```

| Scenario | correction signal | effect |
|-----------------------------------------------|-------------------|------|
| t1 is updating the same tag entry (arriving after t0) | `t0_hitT1Update` | t0 considers t1's `t1_updatedEntry` as a hit → prevent false miss |
| t1 is replacing t0's hit entry with victim | `t0_hitT1Victim` | Forcibly treat t0's hit as a miss → Prevent false hit |

---

### Training input information path

```scala
// Source: ubtb/Bundles.scala:68-70
class MicroBtbMeta(implicit p: Parameters) extends MicroBtbBundle {
  // seems no meta is needed now, reserved for future use
}
```

| Trigger                              | Required Info                                                      | Info Path | Write Port / Conflict Handling |
|--------------------------------------|---------------------------------------------------------------------|-----------|--------------------------------|
| `s3_valid` (fast-train, every s3 cycle) | s3_startPc, finalPrediction (taken, position, target, attribute) | BPU internal `fastTrain` wire (based on `s3_*`) | None (register 1-port, t1 1 time/cycle) |
| `mispredictBranch.valid` (slow mode) | startPc, mispredictBranch (taken, position, target, attribute) | FTQ→BPU `io.train` (commit train payload) | None |

- **MicroBtbMeta is currently not used** (reserved for future use) → No meta storage/consumption path dedicated to uBTB

---

## 1.10 Override and redirection

### Priority rules

```scala
// Source: bpu/Bpu.scala:237-241, 380, 434-441
s3_flush := redirect.valid
s2_flush := s3_flush || s3_override
s1_flush := s2_flush

s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)

s0_startPc := MuxCase(
  s0_startPcReg,
  Seq(
redirect.valid -> redirect.bits.target, // priority: backend redirect
s3_override -> s3_prediction.target, // 2nd priority: mbtb s3 override
s1_valid -> s1_prediction.target // 3rd priority: ubtb/abtb s1 prediction
  )
)
```

```scala
// Source: bpu/Bpu.scala:415-421
io.toFtq.prediction.valid := s1_valid && s2_ready || s3_override
when(s3_override) {
io.toFtq.prediction.bits.fromStage(s3_startPc, s3_prediction) // Use mbtb result
}.otherwise {
io.toFtq.prediction.bits.fromStage(s1_startPc, s1_prediction) // Use ubtb/abtb results
}
```

| Condition              | Winner          | Redirect Target       | Side Effect (Flush/Replay) |
|------------------------|-----------------|-----------------------|----------------------------|
| redirect.valid | Backend redirect | redirect.bits.target | s3_flush → flush entire pipeline |
| s3_override (mbtb ≠ s1) | mbtb s3 results | s3_prediction.target | s2_flush, s1_flush (s1, s2 invalid) |
| !redirect && !s3_override && s1_taken | first taken(min position) among uBTB+ABTB candidates | s1_prediction.target | None |
| !redirect && !s3_override && !s1_taken | FallThrough | fallthrough target | None |

- ubtb miss → FallThroughPredictor is in charge of s1 prediction
- When s3_override occurs, the existing s1 entry in FTQ is updated with the mbtb result (using `s3FtqPtr`)
- MicroRas (uras) can replace the return address (processed simultaneously in s1)
- `s1_taken`/`first taken(min position)` selection is decided from `s1_takenMask`, `CompareMatrix`, and `s1_firstTakenBranch` of BPU top.
  (`bpu/Bpu.scala:266-304`)

---

## Quality Checklist

- [x] Comply with order 1.1~1.10
- [x] specify memory depth/width/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] Specify pair predictor combination rules (utage, uras)
- Includes [x] pseudocode
- [x] latency/throughput quantification (1 cycle, 1 pred/cycle)
- [x] stage input/output timing specified
- [x] indexing/hash expression + PC/history bit position specified
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
- [x] Override/redirection priority and rationale specified
