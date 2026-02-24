# UTage (MicroTage) Prediction Unit Analysis

> 분석 원칙: 모든 내용은 **code-based only**. utage/ 디렉토리(Parameters.scala, Abstracts.scala, Bundles.scala, Helpers.scala, MicroTageTable.scala, MicroTage.scala) 및 bpu/Bundles.scala, bpu/Bpu.scala 코드 기반.

---

## 1.1 Prediction Unit 종류 및 역할

MicroTage는 **TAGE** 계열의 conditional branch direction predictor이다.  
`entry 당 1개의 CFI (conditional branch) 방향 + cfiPosition`을 예측한다.  
예측 대상은 현재 입력 block의 **"다음 block"** 예측 (near-range, non-lookahead).

단, fast-train 전용 경로로 동작: s3 예측 결과(final prediction)를 t0에서 즉시 학습하여 다음 s0 예측에 반영한다.
예측이 유효해지려면 `TakenCounter`가 포화 상태(`isSaturatePositive` 또는 `isSaturateNegative`)여야 한다.

### 쉬운 예시 (uBTB/ABTB와의 결합)

```text
가정:
  BPU s0 입력 PC = PC_A
  다음 block = PC_B, 그 다음 block = PC_C

1) uBTB/ABTB가 PC_B 내부 분기 후보를 제시
   예: (cfiPosition=5, target=PC_C, attribute=Conditional)

2) MicroTage는 target은 예측하지 않고 방향만 예측
   예: (cfiPosition=5, taken=false)

3) BPU top이 cfiPosition 매칭 후 방향 override
   - base(BTB) taken=true, uTAGE taken=false 이면 not-taken으로 뒤집힘
   - 결과: next PC는 PC_C가 아니라 fall-through 쪽으로 선택됨
```

즉, MicroTage는 "어디로 갈지(target)"가 아니라
"BTB가 잡아온 conditional branch를 탈지 말지(direction)"를 보정한다.

```scala
// Source: bpu/utage/MicroTage.scala:97-101
prediction.valid := io.enable && histTableHitMap.reduce(_ || _) &&
  (choseTableTakenCtr.isSaturatePositive || choseTableTakenCtr.isSaturateNegative)
prediction.bits.taken       := finalPredTaken && choseTableTakenCtr.isSaturatePositive
prediction.bits.cfiPosition := finalPredCfiPosition
```

| Unit     | Type | CFI per Entry | Predict Distance | 설명                                                  |
|----------|------|---------------|------------------|-------------------------------------------------------|
| MicroTage | TAGE | 1 (conditional branch) | Next block     | path history 기반 direction + cfiPosition 예측, 포화 counter 활성화 시에만 유효 |

---

## 1.2 Prediction Unit Memory Spec

파라미터 설정 (`Parameters.scala:25-33`):

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

`MicroTageInfo(NumSets, HistoryLength, TagWidth, HistBitsInTag)` 기준:

- **entries** 배열: `RegInit(VecInit(Seq.fill(numSets)(...)))` → 동기 레지스터 (FF-based, not SRAM)
- **usefulEntries** 배열: 별도 분리 저장

### History Length 명시

`TableInfos`의 두 번째 인자가 `HistoryLength`다.

| Table | NumSets | HistoryLength | TagWidth | HistBitsInTag |
|-------|---------|---------------|----------|---------------|
| Table-0 | 512 | **9**  | 9  | 15 |
| Table-1 | 512 | **16** | 12 | 16 |

- MicroTage의 현재 설정 history 길이: **9 / 16**
- 최대 history 길이(현재 활성 table 기준): **16**
- `HistBitsInTag`는 tag 생성 시 반영되는 history bit 수로, `HistoryLength`와 다른 의미다.

```scala
// Source: bpu/utage/MicroTageTable.scala:76-77
private val entries       = RegInit(VecInit(Seq.fill(numSets)(0.U.asTypeOf(new MicroTageEntry))))
private val usefulEntries = RegInit(VecInit(Seq.fill(numSets)(UsefulCounter.Zero)))
```

각 Table의 entry 구성:  
`valid(1) + tag(TagWidth) + takenCtr(3) + cfiPosition(CfiPositionWidth)` + separate `useful(2)`

| Memory/Table | Depth | Width (bit) [entry only] | #Tables | Banks | Read Ports | Write Ports |
|--------------|-------|---------------------------|---------|-------|------------|-------------|
| Table-0 (entries) | 512 | 1 + 9 + 3 + CfiPositionWidth | 1 | 1 (single index) | 1 (combinational) | 1 |
| Table-0 (useful)  | 512 | 2 | 1 | 1 | 1 | 1 |
| Table-1 (entries) | 512 | 1 + 12 + 3 + CfiPositionWidth | 1 | 1 | 1 | 1 |
| Table-1 (useful)  | 512 | 2 | 1 | 1 | 1 | 1 |

> `CfiPositionWidth`는 bpu 공통 파라미터(`HasBpuParameters`)로 정의됨.

---

## 1.3 Prediction Unit Memory Entry 설명

### MicroTageEntry (per-table main entry)

```scala
// Source: bpu/utage/MicroTageTable.scala:68-74
class MicroTageEntry() extends MicroTageBundle {
  val valid:       Bool            = Bool()
  val tag:         UInt            = UInt(tagLen.W)
  val takenCtr:    SaturateCounter = TakenCounter()  // width = TakenCtrWidth = 3
  val cfiPosition: UInt            = UInt(CfiPositionWidth.W)
  // val useful: SaturateCounter = UsefulCounter()  // 분리 저장
}
```

| Field Name   | Width (bit)         | Description                                              |
|--------------|---------------------|----------------------------------------------------------|
| `valid`      | 1                   | 엔트리 유효 여부                                         |
| `tag`        | tagLen (9 or 12)    | PC hash + history hash 기반 태그                         |
| `takenCtr`   | 3 (`TakenCtrWidth`) | Saturating counter: taken 방향 예측                      |
| `cfiPosition`| CfiPositionWidth    | 예측 branch의 block 내 위치                              |

### usefulEntries (별도 배열)

```scala
// Source: bpu/utage/MicroTageTable.scala:77
private val usefulEntries = RegInit(VecInit(Seq.fill(numSets)(UsefulCounter.Zero)))
```

| Field Name | Width (bit)           | Description                           |
|------------|-----------------------|---------------------------------------|
| `useful`   | 2 (`UsefulWidth`)     | 엔트리 유용성 카운터 (allocation 정책에 사용) |

---

## 1.4 Pair BTB Unit 설명

MicroTage는 **aBTB (AheadBTB)** 와 paired 동작한다.

```scala
// Source: bpu/utage/MicroTage.scala:42-43
val abtbPrediction: Vec[Valid[Prediction]] = Input(Vec(NumAheadBtbPredictionEntries, Valid(new Prediction)))

// Source: bpu/Bpu.scala:205
utage.io.abtbPrediction := abtb.io.prediction
```

MicroTage는 aBTB의 conditional taken branch 예측을 base(fallback)로 수신하여:
- s1 단계에서 aBTB의 첫 번째 taken conditional branch (`s1_abtbFirstTakenBranch`)를 메타에 기록.
- training 시 `t0_baseTaken`, `t0_baseCfiPosition`으로 참조해 `useful` 카운터 업데이트 방향을 결정.

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

| Prediction Unit | Paired BTB | Pairing Purpose | 결합 Stage / Signal |
|-----------------|------------|-----------------|---------------------|
| MicroTage       | aBTB       | direction+position fallback (base)로 useful counter 업데이트 방향 판단 | s1 stage: `s1_abtbCondTaken`, `s1_abtbFirstTakenBranch` → `s1_meta.baseTaken / baseCfiPosition` |

---

## 1.5 다음 예측 Pseudocode (paired BTB 포함)

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

## 1.6 Input-to-Output Latency 및 Throughput

MicroTage의 예측 경로:
- **s0**: `computeHash` 및 `entries` 레지스터 읽기 → `io.resp` 유효 (combinational)
- **s0→s1**: `RegEnable(prediction, io.stageCtrl.s0_fire)` → s1에서 `io.prediction` 유효

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

## 1.7 Pipeline Stage 위치 (입력/출력 타이밍)

| Signal                        | Produced @ Stage | Consumed @ Stage | Timing Note                                                     |
|-------------------------------|------------------|------------------|-----------------------------------------------------------------|
| `io.startPc`, `foldedPathHist` | s0               | s0 (combinational) | 즉시 hash 계산, table read                                     |
| `entries[idx]` (read)         | s0 (async read)  | s0               | `RegInit` 배열: FF → read는 combinational                      |
| `io.resp.valid/taken/cfiPos`  | s0               | s0               | combinational hit check                                         |
| `prediction` Wire             | s0               | s0→s1            | `RegEnable(..., s0_fire)`로 s1에 등록                           |
| `io.prediction`               | s1               | BPU top (s1)     | s1 valid signal                                                 |
| `abtbPrediction` (input)      | s1               | s1               | aBTB s0 결과가 s1에 도달; `s1_meta.base*` 에 저장              |
| `s1_meta` / `io.meta`         | s1               | BpuFastTrain (t0) | `s1_meta = RegEnable(predMeta, s0_fire)`; Bpu top이 s1→s2→s3 파이프 후 t0에 전달 |

train 경로에서의 meta 전달:

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

## 1.8 Training 방법

### Training 특성
MicroTage는 **fast-train 전용** predictor이다. resolve(commit-time) train은 없다.

```scala
// Source: bpu/utage/Parameters.scala:59
def EnableFastTrain: Boolean = true
// utage can only be fast-trained, we don't have continous predict block on resolve
```

### Training Trigger
- **t0_fire** = `io.fastTrain.get.valid && io.enable`
- Trigger 조건: s3 예측 결과가 s1 예측과 다를 때 (`hasOverride`) 또는 s3 예측이 확정된 시점에 즉시 학습

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

### FTQ가 보관하는 정보

MicroTage의 meta는 FTQ가 직접 저장하지 않고, Bpu top이 **s1→s2→s3 레지스터 체인**으로 보관 후 `BpuFastTrain`으로 t0에 전달한다.  
(BpuFastTrain은 FTQ를 거치지 않는 BPU 내부 경로)

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
| `histTableHitMap`          | NumTables × 1 = 2 bit              | 각 table hit 여부                         |
| `histTableTakenMap`        | NumTables × 1 = 2 bit              | 각 table의 taken 예측값                   |
| `histTableUsefulVec`       | NumTables × UsefulWidth = 4 bit    | 예측 시점의 useful 값 스냅샷              |
| `histTableCfiPositionVec`  | NumTables × CfiPositionWidth       | 각 table의 cfiPosition 예측값             |
| `baseTaken`                | 1 bit                              | aBTB의 conditional taken 여부 (base)     |
| `baseCfiPosition`          | CfiPositionWidth                   | aBTB의 첫번째 taken branch 위치 (base)   |

### Allocation 정책

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

1. **FastAlloc**: provider table 자신이 useful=0이면 즉시 재활용(provider 교체)
2. **NormalAlloc**: provider보다 높은 인덱스 table 중 useful MSB=0인 후보에서 PriorityEncoder로 선택

### Useful Counter 주기적 Reset

Table-0: `lowTickCounter` (9+1 bit) 오버플로우 시 → `usefulEntries.selfDecrease()` (감소)  
Table-1: `highTickCounter` (11+1 bit) 오버플로우 시 → `usefulEntries.value >>= 1` (우측 시프트)

```scala
// Source: bpu/utage/MicroTage.scala:62-63, 70-73
private val lowTickCounter  = RegInit(0.U((LowTickWidth + 1).W))   // 10 bit
private val highTickCounter = RegInit(0.U((HighTickWidth + 1).W))  // 12 bit
...
case 0 => t.usefulReset := lowTickCounter(LowTickWidth)
case 1 => t.usefulReset := highTickCounter(HighTickWidth)
```

### Write Port / Conflict 처리

- `entries`: update/alloc 경쟁 없음 (per-cycle 1 write, `when(io.update.valid && ...)`)
- `usefulEntries`: update와 usefulReset이 동시 발생 가능하나, `when(io.usefulReset)` 블록이 분리 처리 (Chisel last-connect 시맨틱 → io.usefulReset 우선)
- t0 fast-train은 단일 cycle에 최대 1개 table에 alloc 또는 update → port 충돌 없음

| Trigger         | Required FTQ Info              | FTQ Storage / BPU Internal         | Write Port / Conflict Handling                    |
|-----------------|--------------------------------|------------------------------------|---------------------------------------------------|
| `t0_fire` (fast-train) | `MicroTageMeta` (s1 capture) | BPU내 s1→s2→s3 RegEnable chain + `BpuFastTrain` | 1 write port per table, 충돌 없음; useful reset은 별도 분기 처리 |

---

## 품질 체크리스트

- [x] 1.1~1.8 순서 준수
- [x] memory depth/width/tables/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함
- [x] field별 width/description 표 완성
- [x] pair BTB (aBTB) 결합 규칙 명시
- [x] paired BTB 포함 pseudocode 작성
- [x] latency/throughput 수치화 (1 cycle, 1 pred/cycle)
- [x] stage 입력/출력 타이밍 명시 (s0 input → s1 output)
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
