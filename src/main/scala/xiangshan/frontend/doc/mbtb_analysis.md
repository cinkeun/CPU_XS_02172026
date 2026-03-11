# mbtb (MainBtb) analysis

> Analysis criteria: BTB_analysis_rule.md
> Analysis target: `src/main/scala/xiangshan/frontend/bpu/mbtb/`
> Code-based only — No use of prior knowledge/web-search

---

## 1.1 BTB types and roles

MainBtb is **Region-type BTB**, which indexes SRAM in units of **32B-aligned region**.
All PCs within the same 32B region are mapped to the same SRAM set — `alignOffset` (PC[4:0]) is **excluded** from the SRAM index.
The final prediction result is output in s3, and if it is different from the s1 prediction (ubtb/abtb), **s3_override** is triggered.

**Rationale for Region Structure**:
- SRAM index starts from PC[5] (`alignBankIdx`) → PC[4:0] (`alignOffset`) is only a location inside the region
- This is why the `position` field (CfiAlignedPositionWidth bits) of the entry records the branch location within the region separately.
- Code evidence: `alignOffset` is declared in `addrFields` sequential layout, but index extraction functions such as `getSetIndex`/`getAlignBankIndex` only use PC[5] or higher.

**Align Banking**: Divides the 64B fetch block into two 32B-aligned regions, each of which is handled by a separate alignBank.
This resolves fetch block sorting constraints and provides maximum (banks-1)/banks × predict width coverage.

```
// Source: mbtb/Parameters.scala:30-32
NumAlignBanks: Int = 2,  // FetchBlockSize(64B) / FetchBlockAlignSize(32B) = 2
// maximum (banks-1) / banks * predict width prediction cover possible
```

Prediction distance: **current block → next block** (s3 output, non-lookahead).
Output: `Vec(NumBtbResultEntries=8, Valid[Prediction])` — alignBanks×Ways = 2×4 = 8 slots.

| BTB | Type | Region Width (SRAM index unit) | Predict Distance | Description |
|------|--------|-----------------------------------|---------------------------|------|
| mbtb | Region | FetchBlockAlignSize = 32B | Current block → next block (s3 output) | 8192-entry, 2-level banking, SRAM-based |

---

## 1.2 BTB memory spec

### Structural Hierarchy

```
MainBtb
 ├── MainBtbAlignBank[0]  (alignIdx=0)
 │    ├── MainBtbInternalBank[0] (entrySrams × NumWay, counterSram)
 │    ├── MainBtbInternalBank[1]
 │    ├── MainBtbInternalBank[2]
 │    └── MainBtbInternalBank[3]
 └── MainBtbAlignBank[1]  (alignIdx=1)
      ├── MainBtbInternalBank[0]
      ├── ...
      └── MainBtbInternalBank[3]
```

### SRAM (per MainBtbInternalBank)

```scala
// Source: mbtb/MainBtbInternalBank.scala:90-119
// Per-way entry SRAM (NumWay=4)
private val entrySrams = Seq.tabulate(NumWay) { wayIdx =>
  Module(new SRAMTemplate(
    new MainBtbEntry,
    set = NumSets,   // 256 (= 8192 / 4way / 4internal / 2align)
    way = 1,
    singlePort = true, shouldReset = true, holdRead = true
  ))
}
// Counter SRAM (1 per bank, shared NumWay ways)
private val counterSram = Module(new SRAMTemplate(
  TakenCounter(),
  set = NumSets,   // 256
  way = NumWay,    // 4
  singlePort = true, shouldReset = true, holdRead = true
))
```

### Write buffer

```scala
// Source: mbtb/MainBtbInternalBank.scala:121-133
private val entryWriteBuffer = Module(new WriteBuffer(
  new MainBtbEntrySramWriteReq,
  numEntries = WriteBufferSize,  // 4
numPorts = NumWay // 4 (per-way independent ports)
))
private val counterWriteBuffer = Module(new Queue(
  new MainBtbCounterSramWriteReq,
  WriteBufferSize,  // 4
  pipe = true, flow = true
))
```

| Memory          | Depth   | Width (bit)             | Banks                        | Read Ports | Write Ports |
|-----------------|---------|-------------------------|------------------------------|------------|-------------|
| entrySram (×4 way, ×4 internal, ×2 align = ×32 total) | 256 (NumSets) | MainBtbEntry size | 1/SRAM (single-port, read priority) | 1/SRAM | 1/SRAM (write buffer 4-entry queuing) |
| counterSram (×4 internal, ×2 align = ×8 total) | 256 (NumSets) | TakenCntWidth(2) × NumWay(4) = 8 bit | 1/SRAM (single-port) | 1/SRAM | 1/SRAM (Queue 4-entry queuing) |

- Total physical SRAM: 32 entry SRAM + 8 counter SRAM = 40
- Total logical entries: NumSets(256) × NumWay(4) × NumInternalBanks(4) × NumAlignBanks(2) = **8192**

---

## 1.3 BTB memory entry description

```scala
// Source: mbtb/Bundles.scala:35-55
class MainBtbEntry(implicit p: Parameters) extends MainBtbBundle {
  val valid: Bool = Bool()

  val tag:       UInt            = UInt(TagWidth.W)           // 16-bit tag
  val attribute: BranchAttribute = new BranchAttribute        // 4-bit

  // Relative position to the aligned start addr
  val position: UInt = UInt(CfiAlignedPositionWidth.W)        // CfiPositionWidth - AlignBankIdxLen

  // Branch target info
val targetCarry: TargetCarry = new TargetCarry // 2-bit (always included)
  val targetLowerBits: UInt        = UInt(TargetWidth.W)      // 20-bit
}
```

| Field Name        | Width (bit)               | Description |
|-------------------|---------------------------|-------------|
| valid | 1 | entry validity |
| tag | 16 (TagWidth) | PC[instOffsetBits + ... + TagWidth - 1 : ...], for tag comparison |
| attribute         | 4                         | BranchAttribute (branchType 2-bit + rasAction 2-bit) |
| position | CfiAlignedPositionWidth | **branch location within **region (= CfiPositionWidth - AlignBankIdxLen). Since it is Region BTB, it is necessary to separately record which branch in the region the entry is in. |
| targetCarry | 2 | TargetCarry (Fit/Overflow/Underflow): Always included (not optional unlike ubtb/abtb) |
| targetLowerBits | 20 (TargetWidth) | target lower bit (2B-aligned) |

### AddrField structure

```scala
// Source: mbtb/Helpers.scala:30-45
val addrFields = AddrField(
  Seq(
("alignOffset", FetchBlockAlignWidth), // offset within region (SRAM index not used — Region BTB proof)
    ("alignBankIdx",    AlignBankIdxLen),        // → SRAM index (PC[5:5])
    ("internalBankIdx", InternalBankIdxLen),     // → SRAM index (PC[7:6])
    ("setIdx",          SetIdxLen),              // → SRAM index (PC[15:8])
("tag", TagWidth) // → for tag comparison (PC[31:16])
  ),
  extraFields = Seq(
    ("replacerSetIdx", FetchBlockSizeWidth, SetIdxLen),
    ("targetLower",    instOffsetBits, TargetWidth),
    ("position",       instOffsetBits, FetchBlockAlignWidth),
    ("cfiPosition",    instOffsetBits, FetchBlockSizeWidth)
  )
)
```

- `position` (relative position within alignBank): Value excluding the lower AlignBankIdxLen bit of cfiPosition
- Restore full cfiPosition from s2 to `Cat(s2_posHigherBits, e.position)`

---

## 1.4 Pair prediction unit

```scala
// Source: bpu/Bpu.scala:322-343 (s2 conditional direction decision)
private val s2_condTakenMask = VecInit((s2_mbtbResult zip tage.io.prediction zip s2_scUsed zip s2_scTakenMask).map {
  case (((e, p), useSc), scTaken) =>
    e.valid && e.bits.attribute.isConditional &&
    MuxCase(
e.bits.taken, // default: mbtb counter
      Seq(
useSc -> scTaken, // Sc is active: use Sc result
p.useProvider -> p.providerPred, // Tage provider hit: Tage result
        p.hasAlt      -> p.altPred         // Tage alt hit
      )
    )
})
```

```scala
// Source: bpu/Bpu.scala:356-375 (s3 target decision)
s3_prediction.target :=
  MuxCase(
    s3_fallThroughPrediction.target,
    Seq(
      (s3_taken && s3_useRas)    -> ras.io.topRetAddr,           // RAS: return
      (s3_taken && s3_useIttage) -> ittage.io.prediction.target, // ITTage: indirect
      s3_taken                   -> s3_firstTakenBranch.bits.target  // mbtb: direct/cond
    )
  )
```

| BTB | Paired Unit | Role | Combination method |
|------|-------------|-------------------------------------|-----------|
| mbtb | Tag | Conditional branch direction (provider/alt) | mbtb entry hit → use Tage.providerPred or altPred |
| mbtb | Sc | Tage override (statistical correction) | useSc && Sc.taken ≠ Use Sc result when Tage.taken |
| mbtb | ITTage | Improved indirect branch target accuracy | attribute.needIttage && ittage.hit → use ittage.target |
| mbtb | RAS | return address | attribute.isReturn && s3_taken → use ras.topRetAddr |

---

## 1.5 Next prediction pseudocode

```text
onPredict(startPc):
// --- s0: SRAM read request ---
  s0_rotator = VecRotate(getAlignBankIndex(startPc))
  for i in 0..NumAlignBanks-1:
    alignedStartPc[i] = (i==0) ? startPc : getAlignedPc(startPc + i * alignSize)
  alignBanks[rotated_idx].read(setIdx, posHigherBits, crossPage)

// --- s1: standby ---
// SRAM latency required

// --- s2: Receive response + output prediction ---
  for each alignBank:
    tag = getTag(s2_startPc)
    for each way:
      rawHit = entry.valid && entry.tag == tag
      hit = rawHit && entry.position >= alignedInstOffset && !crossPage
      pred.valid    = hit
      pred.taken    = takenCounter[way].isPositive   // mbtb base direction
      pred.cfiPosition = Cat(posHigherBits, entry.position)
      pred.target   = getFullTarget(s2_startPc, entry.targetLowerBits, entry.targetCarry)
      meta[way] = {rawHit, position, attribute, counter}

// Multi-hit detection → duplicate way flush
  if detectMultiHit(hitMask, positions):
    internalBank.flush(setIdx, multiHitMask)

// --- BPU top s2: Determine direction (mbtb + Tage + Sc) ---
  for each way:
    condTaken = MuxCase(mbtb.taken, [useSc->sc.taken, useProvider->tage.taken, hasAlt->tage.alt])
    jumpTaken = isDirect || isIndirect

// --- s3: Select final prediction ---
  firstTaken = firstTaken(condTaken || jumpTaken)
  target = MuxCase(fallthrough, [isReturn->RAS, needIttage&&ittageHit->ITTage, else->mbtb.target])
  prediction = {taken, cfiPosition, target, attribute}

  // --- s3: replacer touch ---
replacer.predictTouch(setIdx, takenMask) // touch only taken entries
```

---

## 1.6 Input-to-output latency and throughput

| BTB  | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|-------------|--------------|-----------------|--------------------------|
| mbtb | BPU s0 | BPU s3 (prediction valid) | 3 (s0→s1→s2→s3) | 1 (pipeline) |

- s0: SRAM read req sent
- s1: SRAM read resp wait (1-cycle SRAM latency, holdRead=true)
- s2: tag comparison, prediction slot output (`io.result`, `io.meta`)
- s3: replacer touch (uses final takenMask)
- When s3_prediction becomes effective at BPU top: s3_fire

```scala
// Source: mbtb/MainBtb.scala:54-60
private val s0_fire, s1_fire, s2_fire, s3_fire = Wire(Bool())
```

---

## 1.7 Pipeline stage location

| Signal               | Produced @ Stage | Consumed @ Stage | Timing Note |
|----------------------|------------------|------------------|-------------|
| s0_startPcVec | BPU s0 | mbtb s0 (alignBanks) | Distribute to each alignBank after VecRotate |
| alignBank.read.resp  | mbtb s1          | mbtb s2          | `RegEnable(s1_rawEntries, s1_fire)` |
| io.result (8 slots)  | mbtb s2          | BPU s2           | `io.result := VecInit(alignBanks.flatMap(...))` |
| io.meta              | mbtb s2          | BPU s3 (s3_resolveMeta) | `RegEnable(mbtb.io.meta, s2_fire)` |
| s3_prediction | BPU s3 | FTQ | After determining s3_override, send override or s1 result to FTQ |
| io.s3_takenMask | BPU s3 (mbtb+tage+sc combined) | mbtb s3 (replacer) | `mbtb.io.s3_takenMask := s3_takenMask` |

```scala
// Source: mbtb/MainBtbAlignBank.scala:98-117
// s0: read request to internalBank
internalBanks.zipWithIndex.foreach { case (b, i) =>
  b.io.read.req.valid       := s0_fire && s0_internalBankMask(i)
  b.io.read.req.bits.setIdx := s0_setIdx
}
// s1: Obtain internalBank results selected by Mux1H
private val s1_rawEntries  = Mux1H(s1_internalBankMask, internalBanks.map(_.io.read.resp.entries))
private val s1_rawCounters = Mux1H(s1_internalBankMask, internalBanks.map(_.io.read.resp.counters))
// s2: Compare tag after latch
private val s2_rawEntries  = RegEnable(s1_rawEntries, s1_fire)
```

---

## 1.8 BTB memory indexing hashing method

### AddrField layout (sequential bit configuration)

```scala
// Source: mbtb/Helpers.scala:30-45
val addrFields = AddrField(
  Seq(
    ("alignOffset",     FetchBlockAlignWidth),  // PC bit [4:0]   (log2Ceil(32)=5, FetchBlockAlignSize=32B)
    ("alignBankIdx",    AlignBankIdxLen),        // PC bit [5:5]   (log2Ceil(2)=1)
    ("internalBankIdx", InternalBankIdxLen),     // PC bit [7:6]   (log2Ceil(4)=2)
    ("setIdx",          SetIdxLen),              // PC bit [15:8]  (log2Ceil(256)=8, NumSets=256)
    ("tag",             TagWidth)                // PC bit [31:16] (TagWidth=16)
  ),
  maxWidth = Option(VAddrBits),
  extraFields = Seq(
    ("replacerSetIdx", FetchBlockSizeWidth, SetIdxLen),    // PC bit [13:6]  (FetchBlockSizeWidth=6)
    ("targetLower",    instOffsetBits, TargetWidth),       // PC bit [20:1]
    ("position",       instOffsetBits, FetchBlockAlignWidth), // PC bit [5:1]
    ("cfiPosition",    instOffsetBits, FetchBlockSizeWidth)   // PC bit [6:1]
  )
)
```

### Predict path index calculation

```scala
// Source: mbtb/Helpers.scala:47-57
def getSetIndex(pc: PrunedAddr): UInt          = addrFields.extract("setIdx",          pc)
def getAlignBankIndex(pc: PrunedAddr): UInt    = addrFields.extract("alignBankIdx",    pc)
def getInternalBankIndex(pc: PrunedAddr): UInt = addrFields.extract("internalBankIdx", pc)
def getTag(pc: PrunedAddr): UInt               = addrFields.extract("tag",             pc)
def getReplacerSetIndex(pc: PrunedAddr): UInt  = addrFields.extract("replacerSetIdx",  pc)
```

| Field | PC Bits | Width | Parameter | Note |
|-------|---------|-------|-----------|------|
| alignOffset | [4:0] | 5 | FetchBlockAlignWidth=5 | Offset in 32B alignment block, not used for SRAM index |
| alignBankIdx | [5:5] | 1 | AlignBankIdxLen=1 | select alignBank |
| internalBankIdx | [7:6] | 2 | InternalBankIdxLen=2 | Select physical SRAM bank |
| setIdx | [15:8] | 8 | SetIdxLen=8 | SRAM row selection (NumSets=256) |
| tag | [31:16] | 16 | TagWidth=16 | entry comparison |
| replacerSetIdx | [13:6] | 8 | SetIdxLen=8 | PLRU status SRAM index (setIdx and start bit different) |

**replacerSetIdx difference**: Normal `setIdx` = PC[15:8], but `replacerSetIdx` = PC[13:6].
Start at `FetchBlockSizeWidth=6` (bit 6) — Skip alignOffset + alignBankIdx of PC[5:0] and start at the top of the alignBank boundary.

### AlignBank Distribution: VecRotate

```scala
// Source: mbtb/MainBtb.scala (predict section)
// Rotate s0_startPcVec with VecRotate so that each physics alignBank is
// Distribute to receive PCs with alignBankIdx == i
```

- Use history: **None**
- No hash (XOR/fold not applied), PC simple bit extraction

### Train path: cfiPosition → alignBankIdx

```scala
// Source: mbtb/Helpers.scala:56-57
def getAlignBankIndexFromPosition(cfiPosition: UInt): UInt =
  addrFields.extractFrom("cfiPosition", "alignBankIdx", cfiPosition)
// cfiPosition = PC[6:1] (FetchBlockSizeWidth=6 bits, instOffsetBits=1)
// alignBankIdx within cfiPosition = bit[4] of cfiPosition (= PC[5])
```

| Path | Field | Source | PC Bits | History | Note |
|------|-------|------|---------|---------|------|
| Prediction | alignBankIdx | s0_startPc[5:5] | [5:5] | None | Distribution to alignBank with VecRotate |
| Prediction | internalBankIdx | s0_startPc[7:6] | [7:6] | None | |
| Prediction | setIdx | s0_startPc[15:8] | [15:8] | None | |
| Prediction | replacerSetIdx | s0_startPc[13:6] | [13:6] | None | PLRU only |
| Prediction | tag | s0_startPc[31:16] | [31:16] | None | |
| Train | alignBankIdx | getAlignBankIndexFromPosition(cfiPosition) | [5:5] via cfiPos bit 4 | None | Extract from mispredict branch location |
| Train | setIdx | Restore mbtbMeta | — | None | reuse predict point value |
| Train | tag | getTag(t1_train.startPc) | [31:16] | None | |

---

## 1.9 Training method

### Trigger: based on mispredict (commit point, FTQ → BPU train)

```scala
// Source: mbtb/MainBtb.scala:124-151
private val t0_fire  = io.stageCtrl.t0_fire && io.enable
private val t0_train = io.train // BpuTrain from FTQ

// t1: Determine write destination alignBank
private val t1_writeAlignBankIdx  = getAlignBankIndexFromPosition(t1_mispredictInfo.bits.cfiPosition)
private val t1_writeAlignBankMask = t1_rotator.rotate(VecInit(UIntToOH(t1_writeAlignBankIdx).asBools))

alignBanks.zipWithIndex.foreach { case (b, i) =>
  b.io.write.req.valid         := t1_fire && t1_writeAlignBankMask(i)
  b.io.write.req.bits.mispredictInfo := t1_mispredictInfo
}
```

```scala
// Source: mbtb/MainBtbAlignBank.scala:213-222 (entry write condition)
private val t1_entryNeedWrite = t1_mispredictInfo.valid && (
!t1_hit ||                                            // 1. miss: Assign new entry
t1_mispredictInfo.bits.attribute.needIttage ||        // 2. indirect: update target
!(t1_mispredictInfo.bits.attribute === Mux1H(t1_hitMask, t1_meta.map(_.attribute))) // 3. Change attribute
)
```

### Save FTQ meta (MainBtbMeta)

```scala
// Source: mbtb/Bundles.scala:69-80
class MainBtbMetaEntry(implicit p: Parameters) extends MainBtbBundle {
  val rawHit:    Bool            = Bool()
  val position:  UInt            = UInt(CfiPositionWidth.W)
  val attribute: BranchAttribute = new BranchAttribute
val counter: SaturateCounter = TakenCounter() // 2-bit saturate counter

  def hit(branch: BranchInfo): Bool = rawHit && position === branch.cfiPosition
}
class MainBtbMeta(implicit p: Parameters) extends MainBtbBundle {
  val entries: Vec[Vec[MainBtbMetaEntry]] = Vec(NumAlignBanks, Vec(NumWay, new MainBtbMetaEntry))
}
```

### Counter update (t1)

```scala
// Source: mbtb/MainBtbAlignBank.scala:252-263
t1_meta.zipWithIndex.foreach { case (meta, i) =>
  val hitMask    = t1_branches.map { b =>
    b.valid && b.bits.attribute.isConditional && meta.position === b.bits.cfiPosition
  }
  val actualTaken = Mux1H(hitMask, t1_branches.map(_.bits.taken))
  val entryOverridden = t1_entryNeedWrite && t1_entryWayMask(i)

  t1_newCounters(i) := Mux(entryOverridden,
TakenCounter.Weak Positive, // Initialize Weak Positive when assigning to entry
meta.counter.getUpdate(actualTaken) // Increase or decrease according to existing entry: actualTaken
  )
}
// counter is updated for all resolved branches regardless of mispredict
```

| Trigger                           | Required FTQ Info                                  | FTQ Storage                  | Write Port / Conflict Handling |
|-----------------------------------|----------------------------------------------------|------------------------------|--------------------------------|
| t0_fire (FTQ commit → BPU train) | mispredictBranch (valid, position, target, attribute), meta.mbtb (rawHit, position, counter), branches (all resolved) | MainBtbMeta (stored in FTQ resolveMeta) | entry: write buffer (4-entry, per-way port), counter: Queue (4-entry, drop on full) |

- Write request drop when entry write buffer is full (`entry_writebuffer_drop_write` perf counter)
- drop when counter write buffer (Queue) is full (`counter_writebuffer_drop_write` perf counter)
- Priority processing in case of setIdx conflicts such as flush (multi-hit processing) and entry write:

```scala
// Source: mbtb/MainBtbInternalBank.scala:168-176
val conflict = writeEntry.req.valid &&
  writeEntry.req.bits.setIdx === flush.req.bits.setIdx &&
  writeEntry.req.bits.entry.tag === 0.U
// In case of conflict, skip flush, write priority
```

---

## 1.10 Override and redirection

### Conditions for s3_override to occur

```scala
// Source: bpu/Bpu.scala:380
s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)
// s3_prediction: mbtb + Tage + Sc + ITTage + RAS combined final prediction
// s3_s1Prediction: s1 prediction at the time (based on ubtb/abtb)
```

### Flush propagation

```scala
// Source: bpu/Bpu.scala:237-239
s3_flush := redirect.valid
s2_flush := s3_flush || s3_override // s1/s2 flush when s3_override
s1_flush := s2_flush
```

### Select nextPC

```scala
// Source: bpu/Bpu.scala:434-441
s0_startPc := MuxCase(
  s0_startPcReg,
  Seq(
redirect.valid -> redirect.bits.target, // priority: backend redirect
s3_override -> s3_prediction.target, // 2nd priority: mbtb s3 override
s1_valid -> s1_prediction.target // 3rd priority: ubtb/abtb s1 result
  )
)
```

### FTQ override processing

```scala
// Source: bpu/Bpu.scala:415-421
io.toFtq.prediction.valid := s1_valid && s2_ready || s3_override
when(s3_override) {
  io.toFtq.prediction.bits.fromStage(s3_startPc, s3_prediction)
// Overwrite the existing s1 entry of FTQ with mbtb result with s3FtqPtr
}
```

### Replacer update (s3)

```scala
// Source: mbtb/MainBtbAlignBank.scala:190-193
// only touch the taken entry: the not-taken conditional is less useful, so target the victim first
replacer.io.predictTouch.valid        := s3_fire && s3_takenMask.reduce(_ || _)
replacer.io.predictTouch.bits.setIdx  := s3_replacerSetIdx
replacer.io.predictTouch.bits.wayMask := s3_takenMask.asUInt
// s3_takenMask = mbtb + Tage + Sc combined final direction (not just mbtb counter)
```

| Condition                          | Winner          | Redirect Target             | Side Effect (Flush/Replay) |
|------------------------------------|-----------------|-----------------------------|----------------------------|
| redirect.valid(backend) | Backend | redirect.bits.target | s3_flush → entire pipeline flush, no replacer update |
| s3_override (s3 pred ≠ s1 pred) | mbtb s3 results | s3_prediction.target | s2_flush, s1_flush; Overwrite FTQ entry (using s3FtqPtr) |
| mbtb hit, no override | mbtb (fallback to s1) | s1_prediction.target | None (only s3 meta stored in FTQ) |
| mbtb miss (all ways invalid) | FallThrough | fallthrough target | None |
| indirect + ittage hit | mbtb + ITTage | ittage.prediction.target | Replace only target, keep position/attribute mbtb |
| return + RAS valid | mbtb + RAS | ras.topRetAddr | Replace target only |

---

## Quality Checklist

- [x] Comply with order 1.1~1.10
- [x] specify memory depth/width/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] Specify pair predictor combination rules (Tage, Sc, ITTage, RAS)
- Includes [x] pseudocode
- [x] latency/throughput quantification (3 cycle, BPU s3 output)
- [x] stage input/output timing specified
- [x] indexing/hash expression + PC/history bit position specified
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
- [x] Override/redirection priority and rationale specified
