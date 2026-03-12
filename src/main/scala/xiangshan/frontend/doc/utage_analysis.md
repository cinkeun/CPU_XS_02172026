# UTage (MicroTage) Prediction Unit Analysis

> Analysis principle: All content is **code-based only**. utage/ directory (Parameters.scala, Abstracts.scala, Bundles.scala, Helpers.scala, MicroTageTable.scala, MicroTage.scala) and bpu/Bundles.scala, bpu/Bpu.scala code base.

---

## 1.1 Prediction Unit types and roles

MicroTage is a conditional branch direction predictor of the **TAGE** family.
It predicts one CFI (conditional branch) direction per entry, along with `cfiPosition`.
The prediction target is the **"next block"** prediction (near-range, non-lookahead) of the current input block.

However, it operates as a fast-train only path: the s3 prediction result (final prediction) is immediately learned at t0 and reflected in the next s0 prediction.
For the prediction to be valid, `TakenCounter` must be saturated (`isSaturatePositive` or `isSaturateNegative`).

### Easy example (combination with uBTB/ABTB)

```text
home:
BPU s0 input PC = PC_A
Next block = PC_B, next block = PC_C

1) uBTB/ABTB presents PC_B internal branch candidates
Example: (cfiPosition=5, target=PC_C, attribute=Conditional)

2) MicroTage does not predict the target, but only the direction.
Example: (cfiPosition=5, taken=false)

3) BPU top overrides direction after matching cfiPosition
- If base(BTB) taken=true, uTAGE taken=false, it is overturned to not-taken.
- Result: The next PC is selected for fall-through, not PC_C.
```

In other words, MicroTage is not a “target”;
Corrects the “direction of whether to take the conditional branch captured by BTB”.

```scala
// Source: bpu/utage/MicroTage.scala:97-101
prediction.valid := io.enable && histTableHitMap.reduce(_ || _) &&
  (choseTableTakenCtr.isSaturatePositive || choseTableTakenCtr.isSaturateNegative)
prediction.bits.taken       := finalPredTaken && choseTableTakenCtr.isSaturatePositive
prediction.bits.cfiPosition := finalPredCfiPosition
```

| Unit | Type | CFI per Entry | Predict Distance | Description |
|----------|------|---------------|------------------|-------------------------------------------------------|
| MicroTage | TAGE | 1 (conditional branch) | Next block | Path history based direction + cfiPosition prediction, valid only when saturation counter is activated |

---

## 1.2 Prediction Unit Memory Spec

Parameter settings (`Parameters.scala:25-33`):

```scala
// Source: bpu/utage/Parameters.scala:25-39
TableInfos: Seq[MicroTageInfo] = Seq(
  new MicroTageInfo(512, 9, 9, 15),    // Table-0: short history
  new MicroTageInfo(512, 16, 12, 16)   // Table-1: medium/long history (follow Tage)
),
TakenCtrWidth: Int = 3,
NumTables:     Int = 2,
UsefulWidth:   Int = 2,
```

Based on `MicroTageInfo(NumSets, HistoryLength, HistBitsInTag, TagWidth)`:

- **entries** array: `RegInit(VecInit(Seq.fill(numSets)(...)))` → synchronous register (FF-based, not SRAM)
- **usefulEntries** array: stored separately

### Specify History Length

The second argument of `TableInfos` is `HistoryLength`.

| Table | NumSets | HistoryLength (PHR bits) | HistBitsInTag | TagWidth | Taken branches covered |
|-------|---------|--------------------------|---------------|----------|------------------------|
| Table-0 | 512 | **9**  | 9  | 15 | 9 / 2 = **~4** |
| Table-1 | 512 | **16** | 12 | 16 | 16 / 2 = **8** |

- `HistoryLength` = number of **PHR bits** used for index/tag hashing (not branch count)
- Taken branches covered = `HistoryLength / Shamt` (Shamt=2 bits per taken branch)
- Maximum history length (based on currently active table): **16 PHR bits = 8 taken branches**
- `HistBitsInTag` is the number of history bits reflected when creating a tag, and has a different meaning from `HistoryLength`.

```scala
// Source: bpu/utage/MicroTageTable.scala:76-77
private val entries       = RegInit(VecInit(Seq.fill(numSets)(0.U.asTypeOf(new MicroTageEntry))))
private val usefulEntries = RegInit(VecInit(Seq.fill(numSets)(UsefulCounter.Zero)))
```

Entry configuration for each table:
`valid(1) + tag(TagWidth) + takenCtr(3) + cfiPosition(CfiPositionWidth)` + separate `useful(2)`

| Memory/Table | Depth | Width (bit) [entry only] | #Tables | Banks | Read Ports | Write Ports |
|--------------|-------|---------------------------|---------|-------|------------|-------------|
| Table-0 (entries) | 512 | 1 + 9 + 3 + CfiPositionWidth | 1 | 1 (single index) | 1 (combinational) | 1 |
| Table-0 (useful)  | 512 | 2 | 1 | 1 | 1 | 1 |
| Table-1 (entries) | 512 | 1 + 12 + 3 + CfiPositionWidth | 1 | 1 | 1 | 1 |
| Table-1 (useful)  | 512 | 2 | 1 | 1 | 1 | 1 |

> `CfiPositionWidth` is defined as a bpu common parameter (`HasBpuParameters`).

---

## 1.3 Prediction Unit Memory Entry Description

### MicroTageEntry (per-table main entry)

```scala
// Source: bpu/utage/MicroTageTable.scala:68-74
class MicroTageEntry() extends MicroTageBundle {
  val valid:       Bool            = Bool()
  val tag:         UInt            = UInt(tagLen.W)
  val takenCtr:    SaturateCounter = TakenCounter()  // width = TakenCtrWidth = 3
  val cfiPosition: UInt            = UInt(CfiPositionWidth.W)
// val useful: SaturateCounter = UsefulCounter() // Store separately
}
```

| Field Name   | Width (bit)         | Description                                              |
|--------------|---------------------|----------------------------------------------------------|
| `valid` | 1 | Entry validity |
| `tag` | tagLen (9 or 12) | PC hash + history hash based tags |
| `takenCtr` | 3 (`TakenCtrWidth`) | Saturating counter: predicting direction taken |
| `cfiPosition`| CfiPositionWidth | Location of predicted branch in block |

### usefulEntries (separate array)

```scala
// Source: bpu/utage/MicroTageTable.scala:77
private val usefulEntries = RegInit(VecInit(Seq.fill(numSets)(UsefulCounter.Zero)))
```

| Field Name | Width (bit)           | Description                           |
|------------|-----------------------|---------------------------------------|
| `useful` | 2 (`UsefulWidth`) | Entry usefulness counter (used in allocation policy) |

---

## 1.4 Pair BTB Unit Description

MicroTage works paired with **aBTB (AheadBTB)**.

```scala
// Source: bpu/utage/MicroTage.scala:42-43
val abtbPrediction: Vec[Valid[Prediction]] = Input(Vec(NumAheadBtbPredictionEntries, Valid(new Prediction)))

// Source: bpu/Bpu.scala:205
utage.io.abtbPrediction := abtb.io.prediction
```

MicroTage receives aBTB's conditional taken branch prediction as a base (fallback):
- In step s1, the first taken conditional branch (`s1_abtbFirstTakenBranch`) of aBTB is recorded in the meta.
- During training, refer to `t0_baseTaken` and `t0_baseCfiPosition` to determine the `useful` counter update direction.

```scala
// Source: bpu/utage/MicroTage.scala:111-121
private val s1_abtbCondTakenMask = VecInit(io.abtbPrediction.map { pred =>
  pred.valid && pred.bits.taken && pred.bits.attribute.isConditional
})
private val s1_abtbCondTaken          = s1_abtbCondTakenMask.reduce(_ || _)
private val s1_abtbCompareMatrix      = CompareMatrix(VecInit(io.abtbPrediction.map(_.bits.cfiPosition)))
private val s1_abtbFirstTakenBranchOH = s1_abtbCompareMatrix.getLeastElementOH(s1_abtbCondTakenMask)
private val s1_abtbFirstTakenBranch   = Mux1H(s1_abtbFirstTakenBranchOH, io.abtbPrediction)

private val s1_meta = RegEnable(predMeta, 0.U.asTypeOf(Valid(new MicroTageMeta)), io.stageCtrl.s0_fire)
s1_meta.bits.baseTaken       := s1_abtbCondTaken
s1_meta.bits.baseCfiPosition := s1_abtbFirstTakenBranch.bits.cfiPosition
```

| Prediction Unit | Paired BTB | Pairing Purpose | Combined Stage / Signal |
|-----------------|------------|-----------------|---------------------|
| MicroTage | aBTB | Determine useful counter update direction with direction+position fallback (base) | s1 stage: `s1_abtbCondTaken`, `s1_abtbFirstTakenBranch` → `s1_meta.baseTaken / baseCfiPosition` |

---

## 1.5 Next Prediction Pseudocode (with paired BTB)

```text
onPredict(startPc, foldedPathHist, abtbPrediction[]):
  // s0: combinational hash & table lookup
  for each table t in tables:
    (idx, tag) = computeHash(startPc, foldedPathHist, t.tableId)
    entry      = t.entries[idx]
    t.resp.valid       = (entry.tag == tag) && entry.valid
    t.resp.taken       = entry.takenCtr.isPositive
    t.resp.cfiPosition = entry.cfiPosition
    t.resp.useful      = t.usefulEntries[idx]

  // s0: provider selection (highest tableId that hits wins)
  finalTaken       = MuxCase(false, tables.reverse.map(t => t.resp.valid -> t.resp.taken))
  finalCfiPosition = MuxCase(0,     tables.reverse.map(t => t.resp.valid -> t.resp.cfiPosition))
  choseTakenCtr    = MuxCase(Zero,  tables.reverse.map(t => t.resp.valid -> t.resp.hitTakenCtr))

  // s0: prediction valid only if provider counter is saturated
  predValid = enable && histTableHitMap.any() &&
              (choseTakenCtr.isSaturatePositive || choseTakenCtr.isSaturateNegative)
  prediction.valid       = predValid
  prediction.taken       = finalTaken && choseTakenCtr.isSaturatePositive
  prediction.cfiPosition = finalCfiPosition

  // s0->s1 pipeline register (s0_fire)
  s1_predOut = RegEnable(prediction, s0_fire)

  // s1: attach aBTB base info into meta
  abtbCondTakenEntries = abtbPrediction.filter(p => p.valid && p.taken && p.isConditional)
  s1_meta.baseTaken       = abtbCondTakenEntries.any()
  s1_meta.baseCfiPosition = abtbCondTakenEntries.minByPosition().cfiPosition

  output: (s1_predOut, s1_meta)
  // Note: no target output - MicroTage provides direction+cfiPosition only,
  //       target is supplied by aBTB
```

---

## 1.6 Input-to-Output Latency and Throughput

MicroTage’s predictive path:
- **s0**: Read registers `computeHash` and `entries` → `io.resp` valid (combinational)
- **s0→s1**: `RegEnable(prediction, io.stageCtrl.s0_fire)` → `io.prediction` is valid in s1

```scala
// Source: bpu/utage/MicroTage.scala:119, 123
private val s1_meta = RegEnable(predMeta, 0.U.asTypeOf(Valid(new MicroTageMeta)), io.stageCtrl.s0_fire)
io.prediction := RegEnable(prediction, 0.U.asTypeOf(Valid(new MicroTagePrediction)), io.stageCtrl.s0_fire)
io.meta       := s1_meta
```

| Unit      | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|-----------|-------------|--------------|-----------------|-------------------------|
| MicroTage | s0          | s1           | 1               | 1                       |

---

## 1.7 Pipeline Stage Location (Input/Output Timing)

| Signal                        | Produced @ Stage | Consumed @ Stage | Timing Note                                                     |
|-------------------------------|------------------|------------------|-----------------------------------------------------------------|
| `io.startPc`, `foldedPathHist` | s0 | s0 (combinational) | Immediate hash calculation, table read |
| `entries[idx]` (read) | s0 (async read) | s0 | `RegInit` array: FF → read is combinational |
| `io.resp.valid/taken/cfiPos`  | s0               | s0               | combinational hit check                                         |
| `prediction` Wire | s0 | s0→s1 | Registered in s1 as `RegEnable(..., s0_fire)` |
| `io.prediction`               | s1               | BPU top (s1)     | s1 valid signal                                                 |
| `abtbPrediction` (input) | s1 | s1 | aBTB s0 result reaches s1; Save to `s1_meta.base*` |
| `s1_meta` / `io.meta` | s1 | BpuFastTrain(t0) | `s1_meta = RegEnable(predMeta, s0_fire)`; BPU top pipes s1→s2→s3 and then passes to t0 |

Passing meta on the train route:

```scala
// Source: bpu/Bpu.scala:155-157, 187, 316
private val s1_utageMeta = Wire(new MicroTageMeta)
private val s2_utageMeta = RegEnable(s1_utageMeta, s1_fire)
private val s3_utageMeta = RegEnable(s2_utageMeta, s2_fire)
...
fastTrain.bits.utageMeta := s3_utageMeta
...
s1_utageMeta := utage.io.meta.bits
```

---

## 1.8 Training method

### Training Traits
MicroTage is a **fast-train only** predictor. There is no resolve(commit-time) train.

```scala
// Source: bpu/utage/Parameters.scala:59
def EnableFastTrain: Boolean = true
// utage can only be fast-trained, we don't have continous predict block on resolve
```

### Training Trigger
- **t0_fire** = `io.fastTrain.get.valid && io.enable`
- Trigger condition: Immediate learning when the s3 prediction result is different from the s1 prediction (`hasOverride`) or when the s3 prediction is confirmed

```scala
// Source: bpu/utage/MicroTage.scala:127-158
private val t0_fire        = io.fastTrain.get.valid && io.enable
private val t0_trainMeta   = io.fastTrain.get.bits.utageMeta
private val t0_trainData   = io.fastTrain.get.bits.finalPrediction
private val t0_trainOverride = io.fastTrain.get.bits.hasOverride

private val t0_histHitMisPred = t0_predHit && (
  (!t0_trainData.attribute.isConditional && t0_predTaken) ||
  (t0_trainData.attribute.isConditional && (
    (t0_predTaken =/= t0_trainData.taken) ||
    (t0_predCfiPosition =/= t0_trainData.cfiPosition)
  ))
)
private val t0_histMissHitMisPred =
  !t0_predHit && t0_trainData.attribute.isConditional &&
  t0_trainData.taken && t0_fire && io.fastTrain.get.bits.hasOverride

private val t0_misPred             = t0_histHitMisPred || t0_histMissHitMisPred
private val t0_histTableNeedAlloc  = t0_misPred && t0_fire
private val t0_histTableNeedUpdate = t0_predHit && t0_fire
```

### Information stored by FTQ

MicroTage's meta is not stored directly by FTQ, but Bpu top stores it in **s1→s2→s3 register chain** and then transfers it to t0 with `BpuFastTrain`.
(BpuFastTrain is a BPU internal route that does not go through FTQ)

```scala
// Source: bpu/Bundles.scala:252-258
class BpuFastTrain(implicit p: Parameters) extends BpuBundle {
  val startPc:         PrunedAddr    = PrunedAddr(VAddrBits)
  val finalPrediction: Prediction    = new Prediction
  val hasOverride:     Bool          = Bool()
  val abtbMeta:        AheadBtbMeta  = new AheadBtbMeta
  val utageMeta:       MicroTageMeta = new MicroTageMeta
}
```

### Training Meta Fields

```scala
// Source: bpu/utage/Bundles.scala:39-52
class MicroTageMeta(implicit p: Parameters) extends MicroTageBundle {
  val histTableHitMap:         Vec[Bool] = Vec(NumTables, Bool())
  val histTableTakenMap:       Vec[Bool] = Vec(NumTables, Bool())
  val histTableUsefulVec:      Vec[UInt] = Vec(NumTables, UInt(UsefulWidth.W))
  val histTableCfiPositionVec: Vec[UInt] = Vec(NumTables, UInt(CfiPositionWidth.W))
  val baseTaken:               Bool      = Bool()
  val baseCfiPosition:         UInt      = UInt(CfiPositionWidth.W)

  // only for test and debug
  val debug_startVAddr:   Option[UInt] = Option.when(EnableTraceAndDebug)(UInt(VAddrBits.W))
  val debug_useMicroTage: Option[Bool] = Option.when(EnableTraceAndDebug)(Bool())
  val debug_predIdx0:     Option[UInt] = Option.when(EnableTraceAndDebug)(UInt(DebugPredIdxWidth.W))
  val debug_predTag0:     Option[UInt] = Option.when(EnableTraceAndDebug)(UInt(DebugPredTagWidth.W))
}
```

| Field                      | Width                              | Description                              |
|----------------------------|------------------------------------|------------------------------------------|
| `histTableHitMap` | NumTables × 1 = 2 bits | Whether each table hits |
| `histTableTakenMap` | NumTables × 1 = 2 bits | Taken forecast values ​​of each table |
| `histTableUsefulVec` | NumTables × UsefulWidth = 4 bits | Snapshot of useful values ​​at prediction time |
| `histTableCfiPositionVec` | NumTables × CfiPositionWidth | cfiPosition predicted value of each table |
| `baseTaken` | 1 bit | Whether aBTB is conditionally taken (base) |
| `baseCfiPosition` | CfiPositionWidth | aBTB's first taken branch location (base) |

### Allocation Policy

```scala
// Source: bpu/utage/MicroTage.scala:168-176
private val t0_providerMask      = PriorityEncoderOH(t0_trainMeta.histTableHitMap.reverse).reverse
private val t0_histTableNoUseful = t0_trainMeta.histTableUsefulVec.map(useful => useful === 0.U).asUInt
private val t0_fastAllocMask     = t0_providerMask.asUInt & t0_histTableNoUseful
private val hitMask              = t0_trainMeta.histTableHitMap.asUInt
private val lowerFillMask        = Mux(hitMask === 0.U, 0.U, hitMask | (hitMask - 1.U))
private val usefulMask           = t0_trainMeta.histTableUsefulVec.map(useful => useful(UsefulWidth - 1)).asUInt
private val allocCandidateMask   = ~(lowerFillMask | usefulMask)
private val normalAllocMask      = PriorityEncoderOH(allocCandidateMask)
private val t0_allocMask         = Mux(t0_fastAllocMask.orR, t0_fastAllocMask, normalAllocMask)
```

1. **FastAlloc**: If the provider table itself is useful=0, immediately recycle (replace provider)
2. **NormalAlloc**: Select as PriorityEncoder a candidate with useful MSB=0 among the index tables higher than the provider.

### Useful Counter Periodic Reset

Table-0: `lowTickCounter` (9+1 bit) When overflow → `usefulEntries.selfDecrease()` (decrease)
Table-1: `highTickCounter` (11+1 bit) overflow → `usefulEntries.value >>= 1` (right shift)

```scala
// Source: bpu/utage/MicroTage.scala:62-63, 70-73
private val lowTickCounter  = RegInit(0.U((LowTickWidth + 1).W))   // 10 bit
private val highTickCounter = RegInit(0.U((HighTickWidth + 1).W))  // 12 bit
...
case 0 => t.usefulReset := lowTickCounter(LowTickWidth)
case 1 => t.usefulReset := highTickCounter(HighTickWidth)
```

### Write Port/Conflict handling

- `entries`: No update/alloc contention (per-cycle 1 write, `when(io.update.valid && ...)`)
- `usefulEntries`: update and usefulReset can occur simultaneously, but `when(io.usefulReset)` blocks are processed separately (Chisel last-connect semantics → io.usefulReset takes priority)
- t0 fast-train alloc or update up to 1 table in a single cycle → no port conflict

| Trigger         | Required FTQ Info              | FTQ Storage / BPU Internal         | Write Port / Conflict Handling                    |
|-----------------|--------------------------------|------------------------------------|---------------------------------------------------|
| `t0_fire` (fast-train) | `MicroTageMeta` (s1 capture) | In BPU s1→s2→s3 RegEnable chain + `BpuFastTrain` | 1 write port per table, no conflicts; useful reset handles separate branches |

---

## 1.9 Indexing / Hashing Method

### History type: PHR (Path History Register)

uTAGE는 GHR(taken/not-taken 이진 히스토리)이 아니라 **PHR(경로 해시 히스토리)**을 사용한다.

```scala
// Source: bpu/history/phr/Helpers.scala:60-63
def pathHash(pc: PrunedAddr, target: PrunedAddr): UInt = {
  val hash = Cat(pc(9, 1), 0.U(4.W)) ^ target(16, 2) // magic numbers
  hash(PathHashWidth - 1, 0)  // PathHashWidth = 15
}
```

| 항목 | 내용 |
|------|------|
| 히스토리 단위 | 분기 1개 = `pathHash(branchPC, target)` 15-bit 해시값 |
| 업데이트 조건 | taken branch마다 PHR에 shift-in |
| PC 기여 | `pc[9:1]` (9 bits) → 4-bit left-shift 후 사용 |
| Target 기여 | `target[16:2]` (15 bits) |
| PHR 전체 길이 | `nextMultipleOf(MaxHistLen + Shamt*FtqSize + FtqFullFix, 4)` — Shamt=2, FtqSize=64, FtqFullFix=4 |

PHR은 folded form으로만 각 예측기에 전달된다. uTAGE가 받는 `foldedPathHist`는 `PhrAllFoldedHistories` 타입으로, 필요한 `(histLen, foldedLen)` 쌍마다 하나의 `PhrFoldedHistory`를 포함한다.

### Default config 파라미터 (utage/Parameters.scala)

```scala
// Source: bpu/utage/Parameters.scala:25-29
// MicroTageInfo(NumSets, HistoryLength, HistBitsInTag, TagWidth)
new MicroTageInfo(512, 9,  9, 15),  // Table-0
new MicroTageInfo(512, 16, 12, 16)  // Table-1
```

### Folded history 인스턴스 (테이블별)

```scala
// Source: bpu/utage/MicroTageTable.scala:79-81
val idxFhInfo    = FoldedHistoryInfo(histLen, min(log2Ceil(numSets), histLen))
val tagFhInfo    = FoldedHistoryInfo(histLen, min(histLen, histBitsInTag))
val altTagFhInfo = FoldedHistoryInfo(histLen, min(histLen, histBitsInTag - 1))
```

| Table | histLen | histBitsInTag | idxFh (histLen→foldedLen) | tagFh | altTagFh |
|-------|---------|---------------|---------------------------|-------|----------|
| Table-0 | 9 | 9 | 9→**9** (min(9,9)) | 9→**9** | 9→**8** |
| Table-1 | 16 | 12 | 16→**9** (min(9,16)) | 16→**12** | 16→**11** |

### idxFh 증분 업데이트 (PhrFoldedHistory.update)

`idxFh`는 raw PHR에서 매 사이클 새로 읽어 오는 것이 아니라, 이전 `idxFh`에 증분 업데이트를 적용하여 유지한다.
업데이트는 `bpu/history/phr/Bundles.scala`의 `PhrFoldedHistory.update()` 에 의해 수행된다.

```scala
// Source: bpu/history/phr/Bundles.scala:89, 107-148
def needOldestBits: Boolean = info.HistoryLength > info.FoldedLength
```

#### Table-0: histLen=9, foldedLen=9 (`needOldestBits = false`)

`histLen == foldedLen` → 히스토리가 절대 wrap-around하지 않으므로 단순 shift register:

```
newFoldedHist[8:0] = ((idxFh_old << 2) | shiftBits)[8:0]
idxFh_new[8:0]     = newFoldedHist[8:0] ^ computeFoldedHash(Cat(hashHigh, 0.U(2.W)), foldedLen=9)(histLen=9)
```

- `shiftBits = pathHash[1:0]` (Shamt=2 new bits, newest branch at MSB)
- `hashHigh = pathHash[14:2]` (13 bits): `computeFoldedHash`를 통해 9-bit XOR 마스크로 접힘
- `computeFoldedHash(Cat(hashHigh, 00), 9)(9)`: 15-bit 값을 9-bit 청크로 XOR 접기

```scala
// Source: bpu/history/phr/Bundles.scala:138-141 (needOldestBits=false 경로)
((foldedHist << num).asUInt | shiftBits)(info.FoldedLength - 1, 0).asUInt
// + hashFolded:
val hashFolded = computeFoldedHash(Cat(hashHigh, 0.U(maxUpdateNum.W)), info.FoldedLength)(info.HistoryLength)
fh.foldedHist := newFoldedHist ^ hashFolded
```

#### Table-1: histLen=16, foldedLen=9 (`needOldestBits = true`)

`histLen > foldedLen` → 가장 오래된 비트가 wrap-around하여 빠져나가므로 순환 시프트 + oldest-bit XOR-out:

```
// 1. oldest bits 계산 (phr 배열에서 직접 읽음)
oldestBit[0] = phr[histLen-1]  = phr[15]   // 가장 오래된 비트
oldestBit[1] = phr[histLen-2]  = phr[14]   // 두 번째로 오래된 비트

// 2. XOR 단계 (shift 전)
xored = (old foldedHist)
      XOR (wrap-around하는 oldest bits를 해당 foldedLen 내 위치에 XOR-out)
      XOR (shiftBits를 MSB 위치에 XOR-in)

// 3. 순환 왼쪽 시프트 by 2
newFoldedHist[8:0] = circularShiftLeft(xored, 2)

// 4. hashHigh 혼합
idxFh_new[8:0] = newFoldedHist[8:0] ^ computeFoldedHash(Cat(hashHigh, 0.U(2.W)), 9)(16)
```

- `oldestBitPosInFolded = [histLen-1 % foldedLen, histLen-2 % foldedLen] = [15%9, 14%9] = [6, 5]`
- `oldestBitWrapAround = [15/9 > 0, 14/9 > 0] = [true, true]` → 두 oldest bit 모두 XOR-out 대상
- `newestBitsSet`: `shiftBits[1]`을 `foldedLen-1=8` 위치, `shiftBits[0]`을 `foldedLen-2=7` 위치에 XOR-in

#### 공통: `computeFoldedHash`

```scala
// Source: bpu/history/phr/Helpers.scala:65-75
def computeFoldedHash(value: UInt, compLen: Int)(histLen: Int): UInt
// Cat(hashHigh, 0.U(2.W)) = 15-bit 값
// compLen = foldedLen (9)
// histLen = Table-0: 9, Table-1: 16
// → value[histLen-1:0]를 compLen-bit 청크로 쪼개 XOR 접기
```

#### idxFh 갱신 우선순위 (Phr.scala)

```
redirect.valid    → redirectData.foldedPhr   (raw PHR로부터 전체 재계산)
elsewhen s3_override → s3_foldedPhrReg.update()  (증분)
elsewhen s1_valid    → s1_foldedPhrReg.update()  (증분)
otherwise            → s0_foldedPhrReg          (이전 사이클 유지)
```

### computeHash 상세

```scala
// Source: bpu/utage/MicroTageTable.scala:83-98
val unhashedIdx = pc[VAddrBits-1 : instOffsetBits]  // PC[38:1], instOffsetBits=1
val unhashedTag = pc[VAddrBits-1 : PCHighTagStart]  // PC[38:7], PCHighTagStart=7

// --- Index ---
// Case A: idxFh.FoldedLength < log2Ceil(numSets)  →  double-XOR to cover full index width
//   foldShift = log2Ceil(numSets) - idxFhInfo.FoldedLength
//   idx = (unhashedIdx ^ Cat(0[foldShift], idxFh) ^ (idxFh << foldShift))[idxWidth-1:0]
// Case B: idxFh.FoldedLength == log2Ceil(numSets)  →  simple XOR
//   idx = (unhashedIdx ^ idxFh)[idxWidth-1:0]

// --- Tag ---
val lowTag  = (unhashedTag ^ tagFh ^ (altTagFh << 1))[histBitsInTag-1:0]
val highTag = connectPcTag(unhashedIdx, tableId)  // tableId별 PC bit 선택
val tag     = Cat(highTag, lowTag)[tagLen-1:0]
```

**Table-0 index** (idxFhFoldedLen=9 == 9 → Case B, simple XOR):
```
idx[8:0] = (unhashedIdx ^ idxFh[8:0])[8:0]
```

**Table-1 index** (idxFhFoldedLen=9 == 9 → Case B, simple XOR):
```
idx[8:0] = (unhashedIdx ^ idxFh[8:0])[8:0]
```

**Tag 구성:**
```
// Table-0: tagLen=15, histBitsInTag=9
tag[14:0] = Cat(highTag, lowTag)[14:0]

lowTag[8:0]  = (PC[38:7] ^ tagFh[8:0] ^ (altTagFh[7:0] << 1))[8:0]   // 9 bits
highTag      = connectPcTag(unhashedIdx, 0)  // 11 PC bits → Cat → truncated to 6

// Table-1: tagLen=16, histBitsInTag=12
tag[15:0] = Cat(highTag, lowTag)[15:0]

lowTag[11:0] = (PC[38:7] ^ tagFh[11:0] ^ (altTagFh[10:0] << 1))[11:0] // 12 bits
highTag      = connectPcTag(unhashedIdx, 1)  // 10 PC bits → Cat → truncated to 4
```

#### connectPcTag — tableId별 PC bit 선택

```scala
// Source: bpu/utage/Parameters.scala:63-75
// tableId=0 (Short):  unhashedIdx 비트 concat → PC bits {16,14,12,10,8,7,6,5,4,3,2} = 11 bits
PCTagHashBitsForShortHistory  = Seq(15, 13, 11, 9, 7, 6, 5, 4, 3, 2, 1)

// tableId=1 (Medium): unhashedIdx 비트 concat → PC bits {19,17,15,13,11,7,6,5,3,2} = 10 bits
PCTagHashBitsForMediumHistory = Seq(18, 16, 14, 12, 10, 6, 5, 4, 2, 1)
// (unhashedIdx bit i = PC bit i+1)
```

| Table | highTag bits | lowTag bits | tag 총 비트 |
|-------|-------------|-------------|------------|
| Table-0 | 11 (Short PC bits) | 9 | Cat → 20, **truncated to 15** |
| Table-1 | 10 (Medium PC bits) | 12 | Cat → 22, **truncated to 16** |

### Train path

학습 시에는 예측 시점과 다른 PHR 스냅샷(`foldedPathHistForTrain`)으로 동일한 `computeHash`를 재실행한다.

```scala
// Source: bpu/utage/MicroTageTable.scala:117-118
private val (trainIdx, trainTag) =
  computeHash(io.update.bits.startPc, io.update.bits.foldedPathHistForTrain, tableId)
```

`foldedPathHistForTrain`은 BPU top에서 s3 시점 PHR 상태로 전달된다 (`fastTrain.bits.foldedPathHistForTrain`).

### 요약

| 항목 | Table-0 | Table-1 |
|------|---------|---------|
| History type | PHR | PHR |
| HistoryLength (PHR bits) | **9** (~4 taken branches) | **16** (8 taken branches) |
| idxFh 폭 | 9-bit folded | 9-bit folded |
| Index hash | simple XOR (Case B) | simple XOR (Case B) |
| Tag low | PC[38:7] XOR tagFh(9b) XOR (altTagFh(8b)<<1) → 9 bits | PC[38:7] XOR tagFh(12b) XOR (altTagFh(11b)<<1) → 12 bits |
| Tag high (PC bits) | {PC[16,14,12,10,8,7,6,5,4,3,2]} (11b→6b) | {PC[19,17,15,13,11,7,6,5,3,2]} (10b→4b) |
| tagLen | 15 bits | 16 bits |
| History bits used in tag | 9 (histBitsInTag) | 12 (histBitsInTag) |
| Use history for idx | Yes (PHR folded) | Yes (PHR folded) |

---

## Quality Checklist

- [x] Comply with order 1.1~1.9
- [x] specify memory depth/width/tables/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] specify pair BTB (aBTB) combining rules
- Write pseudocode including [x] paired BTB
- [x] latency/throughput quantification (1 cycle, 1 pred/cycle)
- [x] Specify stage input/output timing (s0 input → s1 output)
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
