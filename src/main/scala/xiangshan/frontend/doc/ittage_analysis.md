# ittage (ITTage) analysis

> Analysis criteria: prediction_analysis_rule.md
> Analysis target: `src/main/scala/xiangshan/frontend/bpu/ittage/`
> Code-based only — No use of prior knowledge/web-search

---

## 1.1 Prediction unit types and roles

ITTage is a **Indirect Target TAGE** predictor, which predicts the **target address** of an indirect branch with the `needIttage` attribute.
It uses a TAGE-type multi-tagged table + provider/alt-provider selection structure, and does not include a full target in the table entry.
Save only `targetOffset(offset + pointer + usePcRegion)`.
Here, `pointer` is an index pointing to the target upper bit (region) in a separate `RegionWays` register file (16-entry).

- CFI processing unit: 1 (one indirect branch target per prediction)
- Predicted distance: **current block → next block** (non-lookahead, BPU s3 output)
- Contribution method: Replace the target of the indirect branch detected by mbtb in BPU s3

```scala
// Source: bpu/Bpu.scala:356
private val s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit
```

| Unit | Type | CFI per Entry | Predict Distance | Description |
|--------|------------------|---------------|--------------------------|------|
| ITTage | Indirect-target TAGE | 1 (indirect branch) | Current block → next block (s3 output) | 5-table tagged, provider/alt, RegionWays target compression |

---

## 1.2 prediction unit memory spec

### Tagged Tables (5)

```scala
// Source: ittage/Parameters.scala:23-29
TableInfos: Seq[IttageTableInfo] = Seq(
  new IttageTableInfo(256,  4),   // T0
  new IttageTableInfo(256,  8),   // T1
  new IttageTableInfo(512, 13),   // T2
  new IttageTableInfo(512, 16),   // T3
  new IttageTableInfo(512, 32)    // T4
)
NumBanks:           Int = 2
TagWidth:           Int = 9
ConfidenceCntWidth: Int = 2
UsefulCntWidth:     Int = 1
TargetWidth:        Int = 20     // 2B-aligned
TableSramSize:      Int = 128    // physical SRAM depth per fold
TableWriteBufferSize: Int = 4
```

| Table | Total Rows | HistLen | Banks | Rows/Bank | Physical SRAM depth | Entry Width (bit) | Read Ports | Write Ports |
|-------|-----------|---------|-------|-----------|---------------------|-------------------|------------|-------------|
| T0 | 256 | 4 | 2 | 128 | 128 (foldedWidth=1) | 38 | 1/bank | 1/bank (WriteBuffer 4-entry) |
| T1 | 256 | 8 | 2 | 128 | 128 | 38 | 1/bank | 1/bank |
| T2 | 512 | 13 | 2 | 256 | 128 (foldedWidth=2) | 38 | 1/bank | 1/bank |
| T3 | 512 | 16 | 2 | 256 | 128 | 38 | 1/bank | 1/bank |
| T4 | 512 | 32 | 2 | 256 | 128 | 38 | 1/bank | 1/bank |

- Total physical SRAM: 5 tables × 2 banks = **10** FoldedSRAMTemplate
- Entry width: `1(valid) + 9(tag) + 2(confidenceCnt) + 1(usefulCnt) + 20(offset) + 4(pointer) + 1(usePcRegion) = 38 bits + 1(paddingBit)`
- Banking purpose: Avoid port conflict by using different banks for predict (read) and update (write)

### RegionWays (target region compression)

```scala
// Source: ittage/Parameters.scala:41-43
RegionNums:     Int = 16
RegionPorts:    Int = 2
RegionReplacer: String = "plru"
// Source: ittage/Parameters.scala:74
def RegionBits: Int = VAddrBits - TargetOffsetWidth // Upper bit width
```

| Memory | Depth | Width (bit) | Type | Read Ports | Write Ports | Replacer |
|--------|-------|------------|------|------------|-------------|----------|
| RegionWays | 16 | RegionBits (= VAddrBits - 20) | Register (VecInit) | 5 (predict, pointer-indexed valid-bit) + 2 (update-search, by region content) | 1 (per cycle) | PLRU (write-only touch) |

The exact meaning of the compression method is as follows.

- `RegionWays` is not a “full address space table,” but a **16-entry region dictionary** that contains only recent/frequent target regions.
- Each ITTage table entry does not store the entire upper bits of the target, but refers to the dictionary entry as `pointer(4bit)`.
- If multiple ITTage entries point to the same region, they share the same pointer, thereby reducing redundant storage of high-order bits.
- When restoring, if `respHit(pointer) && !usePcRegion`, use `Cat(regionWays[pointer], offset)`, otherwise fallback to `Cat(PC_region, offset)`.

> **[Caution] predict read `respHit` is a valid-bit check, not a content match**
> ```scala
> // RegionWays.scala:66-67
> io.respHit(i) := regions(io.reqPointer(i)).valid // ← Check only the pointer location valid bit
> io.respRegion(i) := regions(io.reqPointer(i)).region
> ```
> Even after the PLRU evicts the entry and overwrites it to another region, the ITTage entry with the existing pointer remains.
> You can restore the target to **wrong region** by receiving `respHit=true`.
> (misprediction is corrected during FTQ commit)

> **[Caution] PLRU is updated only when writing (predict read is invariant to PLRU state)**
> ```scala
> // RegionWays.scala:88-90
> replacerTouchWays(0).valid := io.writeValid // ← touch only when io.writeValid
> replacerTouchWays(0).bits  := writePointer
> replacer.access(replacerTouchWays)
> ```
> predict read (`reqPointer` path) does not touch the PLRU.
> replacement only reflects the access pattern at the time of train(write).

> **[Note] update-search ports (RegionPorts=2, train route only)**
> ```scala
> // RegionWays.scala:70-79
> val updateTotalHits =
>   VecInit((0 until RegionNums).map(w => regions(w).region === io.updateRegion(i) && regions(w).valid))
> val updateBypass  = (io.updateRegion(i) === io.writeRegion) && io.writeValid
> val updateHit     = updateTotalHits.reduce(_ || _) || updateBypass
> val updatePointer = Mux(updateBypass, writePointer, OHToUInt(updateTotalHits))
> ```
> Unlike predict read(pointer-indexed), the region pointer of provider/altProvider in the train path is
> Two ports (`RegionPorts=2`) that do content-search** with **region value.
> Includes bypass logic for cases where the same region is used simultaneously with writing.

Simple example (conceptual example):

- target = `0x0000_1234_5678`
- Decomposition: `region = target[VAddrBits-1:20]`, `offset = target[19:0]`
- ITTage entry saved values: `offset=0x5678`, `pointer=3`, `usePcRegion=0`
- RegionWays[3] stored value: `region=0x0000_1234`
- Restore on prediction: `Cat(RegionWays[3], 0x5678) = 0x0000_1234_5678`

---

## 1.3 prediction unit memory entry description

### IttageEntry (tagged table storage unit)

```scala
// Source: ittage/Bundles.scala:42-55
class IttageEntry(tagLen: Int)(implicit p: Parameters) extends IttageBundle {
  val valid:         Bool            = Bool()
  val tag:           UInt            = UInt(tagLen.W)
  val confidenceCnt: SaturateCounter = ConfidenceCounter()  // 2-bit
  val targetOffset:  IttageOffset    = new IttageOffset()
val usefulCnt: SaturateCounter = UsefulCounter() // 1-bit (lowest — for bitmask update purpose)
  val paddingBit: UInt            = UInt(1.W)
}

// Source: ittage/Bundles.scala:51-55
class IttageOffset(implicit p: Parameters) extends IttageBundle {
val offset: PrunedAddr = PrunedAddr(TargetOffsetWidth) // target[TargetOffsetWidth-1:0], ≈19bit valid
val pointer: UInt = UInt(log2Ceil(RegionNums).W) // 4-bit → RegionWays index
val usePcRegion: Bool = Bool() // true: Replace high bits with region of PC
}
```

| Field Name | Width (bit) | Description |
|------------|-------------|-------------|
| valid | 1 | entry validity |
| tag | 9 (TagWidth) | PHR folded history based hash tag |
| confidenceCnt | 2 (ConfidenceCntWidth) | target accuracy reliability counter (correct→up, mispred→down) |
| targetOffset.offset | ≈19 (PrunedAddr(20)) | target lower 20-bit (2B-aligned, lowest alignment bit removed) |
| targetOffset.pointer | 4 (log2Ceil(16)) | RegionWays 16-entry dictionary index (target high bits indirectly referenced instead of stored directly) |
| targetOffset.usePcRegion | 1 | If true, configure the upper bits as PC region without looking at RegionWays. Even if it is false, if rTable misses, PC region fallback |
| usefulCnt | 1 (UsefulCntWidth) | TAGE useful bit (reset to 0 upon allocation; periodically reset) |
| paddingBit | 1 | DontCare (for bit-alignment) |

> entry size sanity check: `require(ittageEntrySz == (new IttageEntry(tagLen)).getWidth)`
> `ittageEntrySz = 1 + tagLen + ConfidenceCntWidth + UsefulCntWidth + TargetOffsetWidth + log2Ceil(RegionNums) + 1 = 38`

### RegionEntry (RegionWays storage unit)

```scala
// Source: ittage/RegionWays.scala:42-45
private class RegionEntry(implicit p: Parameters) extends IttageBundle {
  val valid:  Bool = Bool()
val region: UInt = UInt(RegionBits.W) // target upper bits (= VAddrBits - TargetOffsetWidth bits)
}
```

| Field Name | Width (bit) | Description |
|------------|-------------|-------------|
| valid | 1 | entry validity |
| region | RegionBits (= VAddrBits - 20) | High bit of target address (most indirect jumps occur within the same region) |

### IttageMeta (FTQ storage)

```scala
// Source: ittage/Bundles.scala:62-80
class IttageMeta(implicit p: Parameters) extends IttageBundle {
  val valid:             Bool            = Bool()
  val provider:          Valid[UInt]     = Valid(UInt(log2Ceil(NumTables).W))  // 3-bit
  val altProvider:       Valid[UInt]     = Valid(UInt(log2Ceil(NumTables).W))
  val altDiffers:        Bool            = Bool()
  val providerUsefulCnt: SaturateCounter = UsefulCounter()
  val providerCnt:       SaturateCounter = ConfidenceCounter()
  val altProviderCnt:    SaturateCounter = ConfidenceCounter()
  val allocate:          Valid[UInt]     = Valid(UInt(log2Ceil(NumTables).W))
  val providerTarget:    PrunedAddr      = PrunedAddr(VAddrBits)
  val altProviderTarget: PrunedAddr      = PrunedAddr(VAddrBits)
}
```

---

## 1.4 pair BTB unit description

ITTage is paired with **mbtb**.
mbtb detects the indirect branch through the `attribute.needIttage` flag of the entry, and ITTage provides the exact target of the branch.

```scala
// Source: bpu/Bpu.scala:356, 367-374
private val s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit

s3_prediction.target :=
  MuxCase(
    s3_fallThroughPrediction.target,
    Seq(
      (s3_taken && s3_useRas)    -> ras.io.topRetAddr,
(s3_taken && s3_useIttage) -> ittage.io.prediction.target, // ← Replace ITTage target
      s3_taken                   -> s3_firstTakenBranch.bits.target
    )
  )
```

| Prediction Unit | Paired BTB | Pairing Purpose | Combined Stage/Signal |
|-----------------|------------|-----------------|-------------------|
| ITTage | mbtb | indirect branch target refinement | BPU s3/`s3_useIttage = needIttage && ittage.hit` |

- mbtb provides position, attribute, and targetCarry
- ITTage replaces the entire target address (position/attribute remains as mbtb)

---

## 1.5 Next prediction pseudocode (with paired mbtb)

```text
onPredict(s0_startPc, s1_startPc, s1_foldedPhr, s2_mbtbResult):

// --- BPU s1: SRAM read request to 5 tables ---
  for each table T in T0..T4:
    unhashedIdx = s1_startPc >> instOffsetBits
    (bankIdx, setIdx, tag) = computeTagAndHash(unhashedIdx, s1_foldedPhr)
      // bankIdx = unhashedIdx[bankIdxWidth-1:0]
      // setIdx  = (unhashedIdx[bankIdxWidth+setIdxWidth-1:bankIdxWidth] XOR idxFh)[setIdxWidth-1:0]
      // tag     = (unhashedIdx>>idxFullWidth XOR tagFh XOR altTagFh<<1)[tagLen-1:0]
    T.sram[bankIdx].read(setIdx)

// --- BPU s2: resp + provider selection + region inquiry ---
  for each table T:
    entry = T.sram_resp[s1_bankIdx]
    hit   = entry.valid && entry.tag == s1_tag
    T.resp = (hit, entry.confidenceCnt, entry.usefulCnt, entry.targetOffset)

// Parallel Select Two: Select the two longest histories among the hit tables
  provider    = last-index hit table  // longest history
  altProvider = second hit table

// RegionWays lookup: pointer → region upper bits restored
  for each table T:
    if rTable.respHit(T.pointer) && !T.usePcRegion:
      regionTarget(T) = Cat(rTable.respRegion(T.pointer), T.offset)
    else:
regionTarget(T) = Cat(targetGetRegion(s2_startPc), T.offset) // Use PC region

// Select target: Decide whether to use alt based on provider reliability
  providerNull = provider.confidenceCnt.isSaturateNegative
  ittageTarget = if (provided && !providerNull): providerTarget
                 elif (providerNull && altProvided): altProviderTarget
                 else: 0 (no valid target)

  s2_provided    = provided
  s2_ittageTarget = ittageTarget

// --- BPU s3: output ---
  io.prediction.hit    = s3_fire && s3_provided
  io.prediction.target = s3_ittageTarget

// --- BPU top s3: combined with mbtb ---
  if mbtb.firstTakenBranch.attribute.needIttage && ittage.prediction.hit:
finalTarget = ittage.prediction.target // replace mbtb target
  else if mbtb.firstTakenBranch.attribute.isReturn && s3_taken:
    finalTarget = ras.topRetAddr
  else:
    finalTarget = mbtb.target

// meta output (FTQ storage)
  ittageMeta = {valid, provider, altProvider, altDiffers,
                providerCnt, altProviderCnt, providerUsefulCnt,
                allocate, providerTarget, altProviderTarget}
```

---

## 1.6 Input-to-output latency and throughput

```scala
// Source: ittage/IttageTable.scala:129-131
// Inside Table: s0(req) → s1(resp), 1-cycle SRAM latency
private val (s1_setIdx, s1_tag) = (RegEnable(s0_setIdx, io.req.fire), RegEnable(s0_tag, io.req.fire))
private val s1_valid            = RegNext(s0_valid)

// Source: ittage/Ittage.scala:102-111
// Ittage top: s2(table resp wire) → s3(output reg)
private val s3_ittageTarget = RegEnable(s2_ittageTarget, s2_fire)
// io.prediction.hit/target valid at s3_fire
```

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|-------------|--------------|-----------------|--------------------------|
| ITTage | BPU s1 (SRAM req) | BPU s3 (prediction) | 2 (s1→s2→s3) | 1 |

- s1: Send SRAM read request
- s2: SRAM resp + tag compare + provider selection + region search + target assembly
- s3: io.prediction.hit/target valid

---

## 1.7 Pipeline stage location

```scala
// Source: ittage/Ittage.scala:60-67
private val s0_startPc = io.startPc
private val s1_startPc = RegEnable(s0_startPc, s0_fire) // BPU s0→s1 latch
private val s2_startPc = RegEnable(s1_startPc, s1_fire) // BPU s1→s2 latch

// Source: ittage/Ittage.scala:190-193
tables.foreach { t =>
  t.io.req.valid           := s1_fire && s1_isIndirect
  t.io.req.bits.startPc    := s1_startPc
t.io.req.bits.foldedHist := io.s1_foldedPhr // ← Enter PHR folded history in BPU s1
}

// Source: ittage/Ittage.scala:89
private val s2_resps = VecInit(tables.map(t => t.io.resp)) // table resp is valid in BPU s2

// Source: ittage/Ittage.scala:102
private val s3_ittageTarget = RegEnable(s2_ittageTarget, s2_fire) // s2→s3 latch
```

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--------|------------------|------------------|-------------|
| t.io.req (SRAM read req) | BPU s1 | Inside IttageTable s0 | s1_startPc + s1_foldedPhr |
| t.io.resp (SRAM resp + tag hit) | Inside IttageTable s1 (=BPU s2) | Ittage s2 | RegNext(req.fire) internal latch |
| s2_ittageTarget, s2_provided | BPU s2 | — | wire: ParallelSelectTwo + RegionWays lookup |
| io.prediction.hit / target | BPU s3 | BPU top s3 | RegEnable(s2_, s2_fire) |
| io.meta (IttageMeta) | BPU s3 | FTQ (resolveMeta) | ittageMeta := ... at s3_fire |

---

## 1.8 Training method

### Trigger and branch selection

```scala
// Source: ittage/Ittage.scala:116-167
private val t0_fire = io.enable && io.stageCtrl.t0_fire // FTQ commit (same train trigger as mbtb)
private val t1_train = RegEnable(io.train, ..., t0_fire) // t0→t1 1-cycle latch

// train target branch: needIttage && taken only one
val trainBranchIdxVec = VecInit(t1_train.branches.map(b =>
  b.valid && b.bits.attribute.needIttage && b.bits.taken
))
val hasTrainBranch = trainBranchIdxVec.asUInt.orR
assert(PopCount(trainBranchIdxVec) <= 1.U) // max 1

private val updateValid   = hasTrainBranch && RegNext(t0_fire, false.B)
private val updateMisPred = hasTrainBranch && t1_train.branches(trainBranchIdx).bits.mispredict
```

### Update logic (3 cases)

```scala
// Source: ittage/Ittage.scala:336-398

// Case 1: Provider update (updateValid && provider.valid → always performed)
updateMask(provider)          := true.B
updateCorrect(provider)       := providerTarget == realTarget
updateOldCnt(provider) := t1_meta.providerCnt // update confidenceCnt
updateUsefulCnt(provider)     := if !altDiffers: keep
                                  else: (providerTarget == realTarget).asUInt  // 1-bit

// Case 2: Alt-provider penalty (when usedAltPred && updateMisPred)
// → Decrease confidenceCnt of altProvider (correct=false)
updateMask(altProvider) := true.B; updateCorrect(altProvider) := false.B

// Case 3: New entry allocation (updateMisPred && !(providerCorrect && providerUnconf))
when(allocate.valid) {
  updateMask(allocate.bits) := true.B
updateAlloc(allocate.bits) := true.B // → initialized with confidenceCnt = WeakPositive
  updateUsefulCnt(allocate.bits) := UsefulCounter.SaturateNegative  // useful=0
  updateTargetOffset(allocate.bits) := updateRealTargetOffset
}
// Unallocable (usefulCnt all 1) → increase tickCnt; tickCnt saturate → reset all usefulCnt
tickCnt.selfUpdate(!allocate.valid)
when(tickCnt.isSaturatePositive) { updateResetUsefulCnt := true.B }
```

### target encoding (RegionWays write)

```scala
// Source: ittage/Ittage.scala:307-319
private val updatePCRegion = targetGetRegion(t1_train.startPc) // PC upper bits
private val updateRealTargetRegion = targetGetRegion(updateRealTarget) // target upper bits

updateRealTargetOffset.usePcRegion := updateRealUsePCRegion || !updateAlloc.reduce(_ || _)
rTable.io.writeValid               := !updateRealUsePCRegion && updateAlloc.reduce(_ || _)
rTable.io.writeRegion              := updateRealTargetRegion
updateRealTargetOffset.pointer     := rTable.io.writePointer
// PCRegion: target region == PC region → no need to store region bits (displayed as usePcRegion=true)
```

- `writePointer` is determined within RegionWays as "If there is already a region identical to `writeRegion`, its index; if not, it is an invalid slot or PLRU victim."
- In other words, as you asked, the ITTage entry stores the “region dictionary index (pointer)” rather than the “region itself.”

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|---------|------------------|-------------|--------------------------------|
| t0_fire (FTQ commit, mispredict-based) | mispredictBranch (needIttage, taken, target, mispredict), IttageMeta (provider, altProvider, providerTarget, altProviderTarget, providerCnt, altDiffers, allocate) | IttageMeta (FTQ resolveMeta, io.train.meta.ittage) | Each table: per-bank WriteBuffer(4-entry); When a write request is made, SRAM is written in the bank's idle cycle. update_drop perf counter Yes |

- `usefulCnt` exclusive periodic reset: sweep by 1 set/cycle under `usefulCanReset` conditions (Counter-based)
- Bank conflict such as read when writing → read priority (write waits in buffer)

---

## Quality Checklist

- [x] Comply with order 1.1~1.8
- [x] specify memory depth/width/tables/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] specify pair BTB (mbtb) combining rules
- Write pseudocode including [x] paired BTB
- [x] latency/throughput quantification (2 cycle, BPU s3 output)
- [x] stage input/output timing specified
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
