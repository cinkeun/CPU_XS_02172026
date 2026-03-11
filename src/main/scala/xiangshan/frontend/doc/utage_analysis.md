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

Based on `MicroTageInfo(NumSets, HistoryLength, TagWidth, HistBitsInTag)`:

- **entries** array: `RegInit(VecInit(Seq.fill(numSets)(...)))` → synchronous register (FF-based, not SRAM)
- **usefulEntries** array: stored separately

### Specify History Length

The second argument of `TableInfos` is `HistoryLength`.

| Table | NumSets | HistoryLength | TagWidth | HistBitsInTag |
|-------|---------|---------------|----------|---------------|
| Table-0 | 512 | **9**  | 9  | 15 |
| Table-1 | 512 | **16** | 12 | 16 |

- MicroTage's current settings history length: **9 / 16**
- Maximum history length (based on currently active table): **16**
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

## Quality Checklist

- [x] Comply with order 1.1~1.8
- [x] specify memory depth/width/tables/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] specify pair BTB (aBTB) combining rules
- Write pseudocode including [x] paired BTB
- [x] latency/throughput quantification (1 cycle, 1 pred/cycle)
- [x] Specify stage input/output timing (s0 input → s1 output)
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
