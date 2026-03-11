# SC (Statistical Corrector) analysis

> Analysis-based: Scala/Chisel code only (code-based)
> Analysis target: `src/main/scala/xiangshan/frontend/bpu/sc/`

---

## 1.0 Why SC is needed

Bottom line: **Even if the TAGE provider points in the "right direction", the opposite is statistically more likely to be true in certain (PC, history) contexts.**

### Structural limitations of TAGE provider

The final prediction of TAGE is the saturating counter sign in the provider table. When the counter is in `weakTaken(+1)` or `weakNotTaken(-1)`, prediction reliability is low. Even if the counter is saturated, it may be systematically wrong under certain path/history patterns.

### Inverse relationship between TAGE confidence and SC intervention threshold

```scala
// Source: sc/Sc.scala:318-334
val tageConfHigh = s2_providerCtr(i).isSaturatePositive || s2_providerCtr(i).isSaturateNegative
val tageConfMid  = s2_providerCtr(i).isMid
val tageConfLow  = s2_providerCtr(i).isWeak

when(hit && valid && tageConfHigh) {
conf := aboveThreshold(sum, thres >> 1) // threshold ÷ 2 → difficult to intervene
}.elsewhen(hit && valid && tageConfMid) {
  conf := aboveThreshold(sum, thres >> 2)   // threshold ÷ 4
}.elsewhen(hit && valid && tageConfLow) {
conf := aboveThreshold(sum, thres >> 3) // threshold ÷ 8 → Easy intervention
}
```

The weaker the TAGE (uncertainty), the lower the SC threshold, making it easier to override. If the TAGE is saturating (highly confident), the higher the threshold value, making it difficult for the SC to intervene.

### Types of biases SC learns

| table | index configuration | Capturing Bias | Activate |
|---|---|---|---|
| `biasTable` | `(PC, tageProviderIsWeak, tageProviderTaken)` | TAGE's own weak prediction direction bias | **true** |
| `pathTable` | `(PC, path history)` | Branch bias according to call path | **true** |
| `globalTable` | `(PC, GHR)` | Global branching pattern bias | false |
| `bwTable` | `(PC, backward history)` | Loop/reverse pattern bias | false |

The key is that the `biasTable` index includes `tageProviderIsWeak` and `tageProviderTaken`:

```scala
// Source: sc/Sc.scala:287-293
private val s2_biasIdxLowBits = VecInit(s2_providerTakenMask.zip(s2_providerValid).zip(s2_providerCtr).map {
  case ((taken, valid), ctr) => Cat(valid && ctr.isWeak, valid && taken)
})
// biasIdx = Cat(wayIdx, providerIsWeak, providerTaken)
// Directly learn “how often when TAGE predicts weakly taken, it is actually not taken”
```

### Self-calibration threshold

```scala
// Source: sc/Sc.scala:452-456
val scWrong = taken =/= t1_meta.scPred(branchIdx)
val shouldUpdate = writeValid && ...
(t1_meta.tagePred(branchIdx) =/= t1_meta.scPred(branchIdx)) && // only when SC is different from TAGE
  (scWrong || !t1_meta.sumAboveThres(branchIdx))
prevThres.getUpdate(scWrong, en = shouldUpdate)
// If SC is wrong → threshold increases (intervene only when more confident)
// If SC is correct but sum is below threshold → Decrease threshold (intervene more frequently)
```

---

## 1.1 Prediction Unit types and roles

SC (Statistical Corrector) is a corrector that corrects **TAGE prediction**.
When TAGE predicts taken/not-taken for a conditional branch, SC uses the sum (percsum) of multiple signed counter tables to decide whether to overturn the TAGE prediction.

- **Not a stand-alone predictor**: Receives TAGE’s provider prediction + mbtb hit results as input and corrects them.
- **Processing unit**: Processes NumBtbResultEntries (= NumWay × NumAlignBanks = 4 × 2 = **8**) entries of mbtb per-way.
- **Predicted distance**: Direction correction for the conditional branch of the current input block (no lookahead)

| Unit | Type | CFI per Entry | Predict Distance | Description |
|------|------|---------------|-----------------|------|
| SC | Statistical Corrector | 1 (per way) | current block | TAGE taken/not-taken correction |

---

## 1.2 Prediction Unit Memory Spec

### Parameter reference value (`Parameters.scala`)

```scala
// Source: sc/Parameters.scala:22
case class ScParameters(
    PathTableInfos:   Seq[ScTableInfo] = Seq(ScTableInfo(128, 8), ScTableInfo(128, 16)),
    GlobalTableInfos: Seq[ScTableInfo] = Seq(ScTableInfo(128, 8), ScTableInfo(128, 16)),
    BackwardTableInfos: Seq[ScTableInfo] = Seq(ScTableInfo(128, 4), ScTableInfo(128, 8)),
    BiasTableSize:       Int = 128,
    BiasUseTageBitWidth: Int = 2,
    PathEnable:   Boolean = true,
    GlobalEnable: Boolean = false,  // disabled by default
    BWEnable:     Boolean = false,  // disabled by default
    BiasEnable:   Boolean = true,
    CtrWidth:     Int = 6,
    ThresholdWidth: Int = 12,
    ThresholdInit:  Int = 720,
    NumBanks:       Int = 2,
    WriteBufferSize: Int = 4
)
```

### Calculated value

- `NumWays` = `NumBtbResultEntries` = `NumWay × NumAlignBanks` = 4 × 2 = **8**
- `BiasTableNumWays` = `NumWays << BiasUseTageBitWidth` = 8 << 2 = **32**
- `ScEntry` width = `CtrWidth` = **6 bits** (signed saturating counter)

### Memory specification table

| Table | Size (sets) | Width (bits) | #Tables | Banks | Read Ports | Write Ports | Enable |
|-------|------------|--------------|---------|-------|------------|-------------|--------|
| PathTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank (via WriteBuffer) | **true** |
| PathTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | **true** |
| GlobalTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| GlobalTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| BWTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| BWTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| BiasTable | 128 | 6×32=192 per row | 1 | 2 | 1 per bank | 1 per bank | **true** |

> `singlePort = true`: Each bank has one physical read/write port. read first.

### Why separate Path/Global/BW/Bias into “different tables”

The key point is that **even if the SRAM wrapper (`ScTable`) of the same size is reused, the index input and statistics to learn are different**.

- PathTable: indexed by `PC ^ foldedPathHist`. Path history-based correlation learning.
- GlobalTable: Indexed with `PC ^ foldedGHR`. Global history-based correlation learning.
- BWTable: Indexed with `PC ^ foldedBW`. backward (loop) history-based correlation learning.
- BiasTable: Expands the way to `PC`-based set + `wayIdx + (providerWeak/providerTaken)` without history, directly learning **TAGE provider direction bias**.

```scala
// Source: sc/Helpers.scala:37-59
getPathTableIdx   = PC ^ foldedPathHist
getGlobalTableIdx = PC ^ foldedGhr
getBWTableIdx     = PC ^ foldedBW
getBiasTableIdx   = PC only

// Source: sc/Sc.scala:287-293
biasWayIdx = Cat(wayIdx, providerIsWeak, providerTaken)
```

```scala
// Source: sc/Parameters.scala:23-40, 74
PathTableInfos      = [(128, hist=8), (128, hist=16)]
GlobalTableInfos    = [(128, hist=8), (128, hist=16)]
BackwardTableInfos  = [(128, hist=4), (128, hist=8)]
BiasTableNumWays = NumWays << 2 // providerWeak/providerTaken reflects 2 bits
```

In other words, “SRAMs have similar shapes,” but **the learning signal space (feature space) is different, so separate tables** are correct.
In the current default setting, only `Path/Bias` is enabled and `Global/BW` is disabled as a function gate (`GlobalEnable/BWEnable`).

### Why are there two tables of the same type?

The only two fields in `ScTableInfo` are `Size` and `HistoryLength`:

```scala
// Source: bpu/Types.scala:132
class ScTableInfo(val Size: Int, val HistoryLength: Int)
```

Two tables of the same type have the same Size and only different HistoryLength:

| group | [0] Size / HistLen | [1] Size / HistLen |
|---|---|---|
| PathTable | 128 / **8** | 128 / **16** |
| GlobalTable | 128 / **8** | 128 / **16** |
| BWTable | 128 / **4** | 128 / **8** |

Different HistoryLengths result in different index calculations:

```scala
// Source: sc/Sc.scala:153-160
private val s0_pathIdx = PathTableInfos.map(info =>
  getPathTableIdx(
    s0_startPc,
    new FoldedHistoryInfo(info.HistoryLength, min(info.HistoryLength, log2Ceil(info.Size))),
    io.foldedPathHist,
    info.Size
  )
)
// PathTable[0]: PC_high ^ fold(pathHist[7:0],  7bit) → set index
// PathTable[1]: PC_high ^ fold(pathHist[15:0], 7bit) → another set index
```

**Both tables are read simultaneously (in parallel) from s0, and the results are summed**:

```scala
// Source: sc/Sc.scala:235-236
private val s1_pathPercsum =
  VecInit.tabulate(NumWays)(w =>
    s1_pathResp.map(entry => getPercsum(entry(w).ctr.value)).reduce(_ +& _)
  )
// [0].ctr.percsum + [1].ctr.percsum → additive vote, not winner-take-all
```

In other words, the two tables observe the same branch simultaneously with different history lengths and add up the results. This is the same philosophy as TAGE's having multiple history length tables in geometric sequence, where short history (fast learning, narrow context) and long history (slow learning, wide context) complement each other.

The reason why BWTable's history is shorter (4, 8 vs 8, 16): The backward/loop pattern has a short cycle, so a long history is unnecessary.

---

## 1.3 Memory Entry Description

### ScEntry Definition

```scala
// Source: sc/Bundles.scala:44
class ScEntry(implicit p: Parameters) extends ScBundle {
  val ctr: SignedSaturateCounter = Counter()
  // Counter.width = CtrWidth = 6 bits
}

// Source: bpu/SignedSaturateCounter.scala:21
class SignedSaturateCounter(width: Int) extends Bundle {
  val value: SInt = SInt(width.W)
  // range: [-32, 31] for width=6
  // positive (>=0) → predict taken
  // negative (<0)  → predict not-taken
  // weak: value==0 or value==-1
  // mid: !saturate && !weak
  // sat: value==31 or value==-32
}
```

### ScEntry Field Table

| Field Name | Width (bits) | Description |
|-----------|-------------|-------------|
| `ctr` | 6 | Signed saturating counter. `value >= 0` → taken, `< 0` → not-taken. When calculating percsum, expand to `Cat(ctr, 1.U(1.W)).asSInt` = `ctr*2+1` |

### percsum conversion

```scala
// Source: sc/Helpers.scala:61
def getPercsum(ctr: SInt): SInt = Cat(ctr, 1.U(1.W)).asSInt
// Expand the ctr value by 2 times + 1 and add it (remove center bias)
```

### BiasTable Indexing

```scala
// Source: sc/Sc.scala:287
val s2_biasIdxLowBits = Cat(valid && ctr.isWeak, valid && taken)
// [1]: providerValid && providerCtr.isWeak
// [0]: providerValid && providerTaken
// biasWayIdx = Cat(wayIdx[2:0], biasIdxLowBits[1:0])  → 5-bit index into 32 ways
```

### Comparison of entries between PathTable and BiasTable

The ScEntry type is the same, but the table structure is fundamentally different.

```
             PathTable                   BiasTable
             ─────────                   ─────────
Set number 128 128 ← Same
Entry type ScEntry(ctr: 6bit) ScEntry(ctr: 6bit) ← Same
Number of Ways NumWays = 8 BiasTableNumWays = 32 ← 4 times
Set index PC ^ foldedPathHistory PC only (no history)
Way index     cfiPosition[2:0]            Cat(cfiPosition[2:0],
                                              providerIsWeak,
                                              providerTaken)
```

**Reason for 4 times the number of Ways**: `BiasTableNumWays = NumWays << BiasUseTageBitWidth = 8 << 2 = 32`.
Since the lower 2 bits of the way address are filled with the TAGE provider status `(isWeak, taken)`,
Even the same cfiPosition on the same PC accesses **four separate counter slots** depending on the TAGE status.

```scala
// Source: sc/Parameters.scala:74
def BiasTableNumWays: Int = NumWays << BiasUseTageBitWidth  // 8 << 2 = 32

// Source: sc/Sc.scala:287-293
val s2_biasIdxLowBits = Cat(valid && ctr.isWeak, valid && taken)
// bit[1]: providerValid && providerCtr.isWeak
// bit[0]: providerValid && providerTaken

val biasWayIdx = Cat(wayIdx, s2_biasIdxLowBits)
// = Cat( cfiPosition[2:0], providerIsWeak[1], providerTaken[0] )
// → Total 5-bit index → ​​Select 1 of 32 ways
```

What this structure means: Since there is a separate counter for each TAGE provider state,
“Actual results when TAGE predicts weak-taken” and “actual results when saturate-taken” are learned independently.

**Summary of Set index differences**:

```scala
// Source: sc/Helpers.scala:37-59
getPathTableIdx = (PC >> offset) ^foldedPathHist // mix history
getBiasTableIdx = (PC >> offset) // PC only (no history)
```

PathTable XORs the path history and refers to a different set if the call-path is different on the same PC.
BiasTable determines the set only through PC without history, and instead distinguishes TAGE status in the way dimension.

---

## 1.4 Paired BTB Description

SC does not work alone, but is combined with the following two units:

| Prediction Unit | Paired BTB/Predictor | Pairing Purpose | Combined Stage/Signal |
|----------------|---------------------|-------------|------------------|
| SC | mbtb (MainBTB) | Check conditional branch location (hitMask, wayIdx) | s2 / `sc.io.mbtbResult` |
| SC | TAGE | Provider Receives predicted values ​​and confidence (ctr), SC calibrates TAGE | s2 / `sc.io.providerTakenCtrs` |

```scala
// Source: bpu/Bpu.scala:230
sc.io.mbtbResult        := mbtb.io.result
sc.io.providerTakenCtrs := tage.io.toSc.providerTakenCtrVec

// Source: bpu/Bpu.scala:327
s2_condTakenMask = ... MuxCase(e.bits.taken,
  Seq(
useSc -> scTaken, // SC correction priority
    p.useProvider -> p.providerPred, // TAGE provider
    p.hasAlt      -> p.altPred       // TAGE alt
  ))
```

---

## 1.5 Predictive Pseudocode (with paired BTB)

```text
onPredict(startPc, foldedPathHist, commonHR, mbtbResult, providerTakenCtrs):

// --- Stage s0: Table index calculation + SRAM read request ---
  bankMask   = getBankMask(startPc)
  pathIdx[i] = PC[high] ^ foldedHist(PathTableInfos[i].HistLen)   (for i in 0..1)
  globalIdx[i] = PC[high] ^ fold(commonHR.ghr[HistLen-1:0])       (if commonHR.valid)
  bwIdx[i]   = PC[high] ^ fold(commonHR.bw[HistLen-1:0])          (if commonHR.valid)
  biasIdx    = PC[high]

  send read req to: pathTable[i], (globalTable[i] if GlobalEnable),
                    (bwTable[i] if BWEnable), biasTable

// --- Stage s1: Response collection + percsum accumulation (way unit) ---
  for each way w in 0..NumWays-1:
    pathPercsum[w]   = sum(getPercsum(pathResp[i][w].ctr)   for i)
    globalPercsum[w] = sum(getPercsum(globalResp[i][w].ctr) for i)  // 0 if !commonHR.valid
    bwPercsum[w]     = sum(getPercsum(bwResp[i][w].ctr)     for i)  // 0 if !commonHR.valid
    sumPercsum[w]    = pathPercsum[w] + globalPercsum[w] + bwPercsum[w]

// --- Stage s2: mbtb/TAGE combination + threshold judgment ---
  for each way w in mbtbResult:
    if not (mbtbResult[w].valid && isConditional): continue

    wayIdx        = getWayIdx(mbtbResult[w].cfiPosition)
    biasIdxLow    = Cat(providerCtr[w].isWeak && valid, providerTaken[w] && valid)
    biasWayIdx    = Cat(wayIdx, biasIdxLow)
    totalPercsum  = sumPercsum[wayIdx] + biasPercsum[biasWayIdx]

    scPred[w]     = totalPercsum >= 0

    threshold     = scThreshold[wayIdx].value >> 3  // base threshold
    if providerValid && tageConfHigh:
      conf = aboveThreshold(totalPercsum, threshold >> 1)
    elif providerValid && tageConfMid:
      conf = aboveThreshold(totalPercsum, threshold >> 2)
    elif providerValid && tageConfLow:
      conf = aboveThreshold(totalPercsum, threshold >> 3)
    else:
      conf = false

    useScPred[w]     = conf && providerValid && mbtbHit[w]
    sumAboveThres[w] = aboveThreshold(totalPercsum, threshold)

  output:
scTakenMask[w] = scPred[w] // Predict raw direction of SC
scUsed[w] = useScPred[w] // Whether to actually override TAGE
    meta = { scPathResp, scBiasResp, scPred, tagePred, useScPred, sumAboveThres, ... }

// aboveThreshold(sum, thres): |sum| > thres (check with sign)
```

---

## 1.6 Input-to-Output Latency and Throughput

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|------------|-------------|----------------|------------------------|
| SC (scTakenMask, scUsed) | s0 (SRAM req) | s2 (combinatorial output) | 2 | 1 block / cycle |
| SC meta | s0 | s3 (RegEnable of s2_fire) | 3 | 1 block / cycle |

> SRAM: `holdRead=true` → Response from s1 is valid.
> The s2 output is combinatorial (combining the mbtb/TAGE s2 signal to the s1 result).

### Why are `SC(scTakenMask/scUsed)` and `SC meta` latency different?

- `scTakenMask`, `scUsed` are calculated directly in s2 and output immediately.

```scala
// Source: sc/Sc.scala:342-343
io.scTakenMask := s2_scPred
io.scUsed      := s2_useScPred
```

- `meta` registers **s2 results once more** for FTQ training and delivers them to s3.

```scala
// Source: sc/Sc.scala:349-360
io.meta.scPathResp      := ... RegEnable(..., s2_fire)
io.meta.scPred          := RegEnable(s2_scPred, s2_fire)
io.meta.useScPred       := RegEnable(s2_useScPred, s2_fire)
io.meta.sumAboveThres   := RegEnable(s2_sumAboveThres, s2_fire)
```

organize:
- The prediction decision signal (`scTakenMask/scUsed`) is used directly in the final MUX of BPU s2, so latency 2.
- Learning meta (`meta`) is transferred to s3 for stage matching/FTQ storage, so latency 3.

---

## 1.7 Pipeline Stage Location

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--------|-----------------|-----------------|-------------|
| `s0_pathIdx`, `s0_biasIdx` | s0 | s0 (SRAM req) | combinatorial from startPc + foldedPathHist |
| `s0_globalIdx`, `s0_bwIdx` | s0 | s0 (SRAM req) | gated by `commonHR.valid` |
| `s1_pathResp`, `s1_biasResp` | s1 | s1 (percsum calculation) | SRAM 1-cycle read latency |
| `s1_sumPercsum[w]` | s1 | s2 (RegEnable s1_fire) | path+global+bw sum |
| `mbtbResult` input | s2 | s2 | mbtb result same cycle |
| `providerTakenCtrs` input | s2 | s2 | TAGE s2 output |
| `scTakenMask`, `scUsed` | s2 | s2 (Bpu.scala combined) | s2 combinatorial output |
| `meta.*` | s2 → s3 | FTQ (training) | RegEnable(s2_fire) |
| `scThreshold[w]` | trained t1 | s2 | Reg, updated at t1 |

```scala
// Source: sc/Sc.scala:57-60
private val s0_fire = io.stageCtrl.s0_fire && io.enable
private val s1_fire = io.stageCtrl.s1_fire && io.enable
private val s2_fire = io.stageCtrl.s2_fire && io.enable
private val s3_fire = io.stageCtrl.s3_fire && io.enable

// Source: sc/Sc.scala:95
private val s1_commonHR = RegEnable(s0_commonHR, s0_fire)
private val s2_commonHR = RegEnable(s1_commonHR, s1_fire)

// Up to 3 cycles backfill with state machine when GHR override
// idle → state1 → state2 → state3 → idle
```

---

## 1.8 Training method

### Training Trigger

- FTQ resolve → `t0_fire` (= `io.stageCtrl.t0_fire`)
- t1: `t1_train = RegEnable(io.train, t0_fire)` → Process after 1 cycle delay

### Training conditions

```scala
// Source: sc/Helpers.scala:84
val needUpdate = writeValid && writeWayIdx === wayIdx &&
  metaData.tagePredValid(branchIdx) &&
  (metaData.scPred(branchIdx) =/= writeTaken || !metaData.sumAboveThres(branchIdx))
// Condition: When the TAGE provider is valid and the SC prediction is incorrect or the SC sum is below the threshold
```

### Threshold update conditions

```scala
// Source: sc/Sc.scala:452-456
val shouldUpdate = writeValid && writeWayIdx === wayIdx &&
  metaData.tagePredValid(branchIdx) &&
  (metaData.tagePred(branchIdx) =/= metaData.scPred(branchIdx)) &&
  (scWrong || !metaData.sumAboveThres(branchIdx))
prevThres.getUpdate(scWrong, en = shouldUpdate)
// scWrong=true → reduce threshold (use SC more often)
// scWrong=false → increase threshold (more rarely use SC)
```

### FTQ Archive Information

```scala
// Source: sc/Bundles.scala:69
class ScMeta(implicit p: Parameters) extends ScBundle with HasScParameters {
  val scPathResp:      Vec[Vec[UInt]] // NumPathTables × NumWays × 6 bits
  val scGlobalResp:    Vec[Vec[UInt]] // NumGlobalTables × NumWays × 6 bits
  val scBWResp:        Vec[Vec[UInt]] // NumBWTables × NumWays × 6 bits
  val scBiasResp:      Vec[UInt]      // BiasTableNumWays(=32) × 6 bits
val scBiasLowerBits: Vec[UInt] // NumWays × 2 bits (TAGE confidence lower bits)
val scCommonHR: CommonHREntry // ghr/bw history (for index recalculation during training)
val scPred: Vec[Bool] // NumWays: SC raw direction prediction
val tagePred: Vec[Bool] // NumBtbResultEntries: TAGE provider prediction
  val tagePredValid:   Vec[Bool]      // NumBtbResultEntries: TAGE provider valid
val useScPred: Vec[Bool] // NumWays: Whether SC actually overrides
val sumAboveThres: Vec[Bool] // NumWays: Is the sum above the threshold?
}
```

### Training flow summary

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|---------|-------------------|-------------|-------------------------------|
| FTQ resolve (t0_fire) | scPathResp, scBiasResp, scCommonHR, scPred, tagePred, tagePredValid, useScPred, sumAboveThres | `io.train.meta.sc` (ScMeta) | WriteBuffer(size=4) per bank. read first: `bank.io.w.req.valid := buffer.valid && !bank.io.r.req.valid` |

### WriteBuffer operation

```scala
// Source: sc/ScTable.scala:108-113
sram.zip(writeBuffer).foreach { case (bank, buffer) =>
  bank.io.w.req.valid  := buffer.io.read.head.valid && !bank.io.r.req.valid
// Write only when there is no read. Wait in WriteBuffer when read crashes
  buffer.io.read.head.ready := bank.io.w.req.ready && !bank.io.r.req.valid
}
```

- **write priority**: Write only when there is no read request → read-first
- **Conflict handling**: WriteBuffer(FIFO, depth=4) stores pending writes
- **wayMask-based selective write**: Selectively update only the changed way to `wayMask`

---

## Quality Checklist

- [x] Comply with order 1.1~1.8
- [x] specify memory depth/width/tables/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] specify pair BTB combining rules
- Write pseudocode including [x] paired BTB
- [x] latency/throughput quantification
- [x] stage input/output timing specified
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
