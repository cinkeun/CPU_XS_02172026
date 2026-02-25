# TAGE Prediction Unit Analysis

> 분석 원칙: 모든 내용은 **code-based only**. tage/ 디렉토리(Parameters.scala, Abstracts.scala, Bundles.scala, Helpers.scala, TageTable.scala, Tage.scala) 및 bpu/Bundles.scala, bpu/Bpu.scala 코드 기반.

---

## 1.1 Prediction Unit 종류 및 역할

TAGE는 **TAgged GEometric history length predictor** 계열의 conditional branch direction predictor이다.  
`entry 당 1개의 CFI (conditional branch) 방향`을 예측하며, block 내 각 branch position별로 독립적으로 prediction을 생성한다 (`NumBtbResultEntries`개).  
예측 대상은 현재 입력 block의 **"다음 block"** 예측 (mBTB가 제공하는 branch candidate에 대해 direction 보완).

- 8개 테이블: history length 4~397로 기하급수적 증가
- 예측 로직: 가장 긴 history를 가진 hit table = provider, 그 다음 = alt
- `useAltOnNa`: provider counter가 weak(약한 saturation)이고 `useAltOnNaVec`이 positive면 alt 사용
- SC (Statistical Corrector)와 연동: `tage.io.toSc.providerTakenCtrVec`으로 provider counter 전달

```scala
// Source: bpu/tage/Tage.scala:149-155
val useProvider = hasProvider && (!useAltOnNa || !provider.takenCtr.isWeak)
io.prediction(i).useProvider  := useProvider
io.prediction(i).providerPred := provider.takenCtr.isPositive
io.prediction(i).hasAlt       := hasAlt
io.prediction(i).altPred      := alt.takenCtr.isPositive
```

| Unit | Type | CFI per Entry | Predict Distance | 설명 |
|------|------|---------------|-----------------|------|
| TAGE | TAGE (8 tables, geometric history) | 1 conditional branch per position | Next block (per mBTB candidate) | provider/alt 선택, SC 후처리 입력 제공 |

---

## 1.2 Prediction Unit Memory Spec

파라미터 (`Parameters.scala:23-45`):

```scala
// Source: bpu/tage/Parameters.scala:24-44
TableInfos: Seq[TageTableInfo] = Seq(
  new TageTableInfo(4096, 2, 4),    // Table-0: histLen=4
  new TageTableInfo(4096, 2, 9),    // Table-1: histLen=9
  new TageTableInfo(4096, 2, 17),   // Table-2: histLen=17
  new TageTableInfo(4096, 2, 29),   // Table-3: histLen=29
  new TageTableInfo(4096, 2, 56),   // Table-4: histLen=56
  new TageTableInfo(4096, 2, 109),  // Table-5: histLen=109
  new TageTableInfo(4096, 2, 211),  // Table-6: histLen=211
  new TageTableInfo(4096, 2, 397)   // Table-7: histLen=397
),
NumBanks:       Int = 4,
TagWidth:       Int = 13,
TakenCtrWidth:  Int = 3,
UsefulCtrWidth: Int = 2,
WriteBufferSize: Int = 4,
```

`TageTableInfo(Size, NumWays, HistoryLength)`: Size=4096 = NumSets × NumBanks × NumWays  
→ NumSets = 4096 / (4 banks × 2 ways) = **512 sets per bank**

각 table은:
- **entrySram**: `SRAMTemplate`, single-port, `NumBanks × NumWays = 4 × 2 = 8`개 SRAM instance
- **usefulCtrs**: `RegInit` 3차원 배열 (FF-based, SRAM 아님)

```scala
// Source: bpu/tage/TageTable.scala:49-73
private val entrySram =
  Seq.tabulate(NumBanks, NumWays) { (bankIdx, wayIdx) =>
    Module(new SRAMTemplate(
      new TageEntry, set = NumSets, way = 1,
      singlePort = true, ...
    ))
  }
private val usefulCtrs = RegInit(
  VecInit.fill(NumBanks)(VecInit.fill(NumWays)(VecInit.fill(NumSets)(UsefulCounter.Zero)))
)
```

**Write Buffer**: bank별 1개, size=4, `numPorts=NumWays=2`  
→ SRAM single-port conflict 완화를 위해 write 요청을 write buffer에서 read가 없을 때 drain

| Memory/Table | Depth | Width (bit) | #Tables | Banks | Read Ports | Write Ports (via WB) |
|---|---|---|---|---|---|---|
| entrySram (각 bank, way) | 512 (sets) | 1+13+3 = 17 | 8 | 4 | 1 (shared predict/train, mutex) | 1 (single-port SRAM, via WriteBuffer) |
| usefulCtrs (FF) | 512 (sets) | 2 | 8 × 4 banks × 2 ways | - | 1 | 1 |
| useAltOnNaVec | NumUseAltOnNa=128 | 7 | - | - | 1 | 1 |
| usefulResetCtr | 1 | 8 | - | - | - | 1 |

> predict read / train read가 **동일 SRAM single port**를 공유하므로 `assert(!(predictReadValid && trainReadValid))` — bank conflict 시 `io.trainReady := false`로 stall

---

## 1.3 Prediction Unit Memory Entry 설명

### TageEntry (SRAM per way)

```scala
// Source: bpu/tage/Bundles.scala:54-58
class TageEntry(implicit p: Parameters) extends TageBundle {
  val valid:    Bool            = Bool()              // 1 bit
  val tag:      UInt            = UInt(TagWidth.W)   // 13 bit
  val takenCtr: SaturateCounter = TakenCounter()     // 3 bit (TakenCtrWidth)
}
```

| Field Name | Width (bit) | Description |
|---|---|---|
| `valid` | 1 | 엔트리 유효 여부 |
| `tag` | 13 (`TagWidth`) | PC + position XOR folded history hash 기반 태그 |
| `takenCtr` | 3 (`TakenCtrWidth`) | Saturating taken counter |

### 별도 usefulCtrs (FF)

| Field Name | Width (bit) | Description |
|---|---|---|
| `usefulCtr` | 2 (`UsefulCtrWidth`) | 유용성 카운터, provider가 alt보다 나을 때 increment |

### TageMetaEntry (FTQ에 저장하는 meta)

```scala
// Source: bpu/tage/Bundles.scala:108-115
class TageMetaEntry(implicit p: Parameters) extends TageBundle {
  val useProvider:       Bool            = Bool()
  val providerTableIdx:  UInt            = UInt(TableIdxWidth.W)    // log2(8) = 3 bit
  val providerWayIdx:    UInt            = UInt(MaxNumWays.W)       // 2 bit
  val providerTakenCtr:  SaturateCounter = TakenCounter()           // 3 bit
  val providerUsefulCtr: SaturateCounter = UsefulCounter()          // 2 bit
  val altOrBasePred:     Bool            = Bool()                   // 1 bit
}
```

| Field Name | Width (bit) | Description |
|---|---|---|
| `useProvider` | 1 | provider 예측 사용 여부 |
| `providerTableIdx` | 3 | provider table 인덱스 |
| `providerWayIdx` | 2 | provider way 인덱스 |
| `providerTakenCtr` | 3 | 예측 시점의 provider counter 값 |
| `providerUsefulCtr` | 2 | 예측 시점의 provider useful counter 값 |
| `altOrBasePred` | 1 | alt pred 또는 base(mBTB) pred |

---

## 1.4 Pair BTB Unit 설명

TAGE는 **mBTB (MainBTB)** 와 paired 동작한다.

```scala
// Source: bpu/tage/Tage.scala:38-41
val fromMainBtb: MainBtbToTageIO = new MainBtbToTageIO
// Source: bpu/Bpu.scala:222
tage.io.fromMainBtb.result := mbtb.io.result
```

- mBTB가 s2에서 branch candidates(`NumBtbResultEntries`개)를 제공
- TAGE는 각 branch candidate의 `cfiPosition`을 tag에 XOR하여 per-branch 예측 생성
- mBTB miss 시 base prediction = mBTB entry의 taken 값 → `altOrBasePred`에 저장
- train 시 TAGE는 mBTB의 `meta.mbtb.entries`를 lookup하여 해당 branch의 counter (base pred) 참조

```scala
// Source: bpu/tage/Tage.scala:117-125
s2_branches.zipWithIndex.foreach { case (branch, i) =>
  val position = branch.bits.cfiPosition
  val tag      = s2_rawTag(tableIdx) ^ position  // position is XOR'd into tag
  ...
  io.meta.entries(i).altOrBasePred := Mux(hasAlt, alt.takenCtr.isPositive, branch.bits.taken)
}
```

| Prediction Unit | Paired BTB | Pairing Purpose | 결합 Stage/Signal |
|---|---|---|---|
| TAGE | mBTB (MainBTB) | branch candidates 수신, direction 보완, base pred 제공 | s2: `io.fromMainBtb.result` → per-branch tag XOR position, altOrBasePred fallback |

---

## 1.5 다음 예측 Pseudocode (paired BTB 포함)

```text
onPredict(startPc, foldedPathHist, mBTBresult[]):

  // s0: SRAM read request 발송
  for each table t:
    bankIdx = getBankIndex(startPc)
    setIdx  = getSetIndex(startPc, t.foldedHist.forIdx)
    t.entrySram[bankIdx].sendReadReq(setIdx)
  
  // s1: SRAM read resp 수신 (1 cycle latency), rawTag 계산
  for each table t:
    rawTag[t] = getTag(startPc) XOR t.foldedHist.forTag

  // s2: mBTB result 수신, per-branch prediction
  for each branch b in mBTBresult:
    position = b.cfiPosition
    useAltOnNa = useAltOnNaVec[getUseAltOnNaIdx(cfiPc(startPc, position))].isPositive

    for each table t:
      tag = rawTag[t] XOR position    // position hashed into tag
      hit[t] = (readEntry.valid && readEntry.tag == tag)
      takenCtr[t] = readEntry.takenCtr (from 1-cycle delayed SRAM resp)

    // provider = longest history hit table
    providerTableOH = PriorityEncoderOH(hitTableMask.reverse).reverse
    provider = Mux1H(providerTableOH, allTableResults)
    alt      = second longest history hit table

    useProvider = hasProvider && (!useAltOnNa || !provider.takenCtr.isWeak)
    providerPred = provider.takenCtr.isPositive
    altPred      = alt.takenCtr.isPositive

    output prediction[b]:
      useProvider  = useProvider
      providerPred = providerPred
      hasAlt       = hasAlt
      altPred      = altPred

    output toSc[b]:
      providerTakenCtr = provider.takenCtr  // SC가 TAGE counter를 참조

    output meta[b]:
      providerTableIdx, providerWayIdx, providerTakenCtr, providerUsefulCtr, altOrBasePred
```

---

## 1.6 Input-to-Output Latency 및 Throughput

예측 파이프라인:
- **s0**: startPc 수신 → SRAM read req 발송 (`s0_fire && io.enable`)
- **s1**: SRAM read resp 수신 (1 cycle), rawTag 계산 (`RegEnable(s0_*)`)
- **s2**: mBTB result 수신, tag match, provider 선택, prediction 출력 (`RegEnable(s1_*)`)

```scala
// Source: bpu/tage/Tage.scala:93-95, 111-113
private val s1_startPc    = RegEnable(s0_startPc, s0_fire)
private val s1_foldedHist = RegEnable(s0_foldedHist, s0_fire)
private val s2_startPc  = RegEnable(s1_startPc, s1_fire)
private val s2_rawTag   = RegEnable(s1_rawTag, s1_fire)
private val s2_readResp = RegEnable(s1_readResp, s1_fire)
```

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|--|--|--|--|--|
| TAGE | s0 | s2 | 2 | 1 (NumBtbResultEntries branches per cycle) |

---

## 1.7 Pipeline Stage 위치 (입력/출력 타이밍)

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--|--|--|--|
| `io.startPc`, `foldedPathHist` | s0 | s0 | SRAM read req 발송 |
| SRAM read resp | s1 | s1 | `DataHoldBypass(tables.map(_.io.predictReadResp), RegNext(s0_fire))` |
| `s1_rawTag` | s1 | s2 (`RegEnable(s1_fire)`) | tag 계산 후 s2에 등록 |
| `io.fromMainBtb.result` | s2 | s2 | mBTB s2 결과가 TAGE s2에 동시 도착, position → tag XOR |
| `io.prediction[]` | s2 | s2 (BPU top s2) | s2에서 바로 유효 |
| `io.meta` | s2 | BPU top s3 (`RegEnable(tage.io.meta, s2_fire)`) | s3에서 FTQ에 저장 |
| `io.toSc.providerTakenCtrVec` | s2 | SC (s2) | SC가 s2에서 TAGE counter 수신 |

train 경로:

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--|--|--|--|
| `io.train` (from FTQ resolve) | t0 | t0 | resolve time branch info + BpuResolveMeta |
| SRAM train read req | t0 | t0 | `t0_fire && !t0_useMeta && !bankConflict` |
| train SRAM read resp | t1 | t1 | `RegEnable(t0_fire)` |
| t2 update/alloc write | t2 | t2 (→ WriteBuffer) | `RegEnable(t1_fire)`, WriteBuffer를 통해 SRAM write |

---

## 1.8 Training 방법

### Training Trigger
- **t0_fire** = `io.stageCtrl.t0_fire && t0_hasCond && io.enable`
- Trigger: FTQ로부터 resolve 완료 후 (`BpuTrain`) — actual taken/mispredict 결과 수신
- **Fast-train 없음**: TAGE는 resolve (commit) 기반 train만 수행

```scala
// Source: bpu/tage/Tage.scala:189
private val t0_fire = io.stageCtrl.t0_fire && t0_hasCond && io.enable
```

### Meta 재사용 (`useMeta`) 최적화

```scala
// Source: bpu/tage/Tage.scala:202-210
private val t0_useMeta = t0_branches.zipWithIndex.map { case (branch, i) =>
  val mbtbHit      = t0_mbtbHitMask(i)
  val isCond       = t0_condMask(i)
  val useProvider  = t0_meta(i).useProvider
  val mispredicted = branch.bits.mispredict
  !(mbtbHit && isCond) || (useProvider && !mispredicted)
}.reduce(_ && _)
private val t0_needRead = !t0_useMeta
```

- 모든 conditional branch that hit mBTB가 provider를 사용하고 misprediction이 없으면 → **meta에서 직접 읽어 SRAM read 생략** (`t0_useMeta = true`)
- 하나라도 mispredict 혹은 non-provider path면 → SRAM re-read 수행

### FTQ가 보관해야 하는 정보

```scala
// Source: bpu/Bundles.scala:278-286, bpu/Bpu.scala:401
class BpuResolveMeta(implicit p: Parameters) extends BpuBundle {
  val mbtb:   MainBtbMeta = new MainBtbMeta
  val tage:   TageMeta    = new TageMeta    // ← TAGE meta
  val sc:     ScMeta      = new ScMeta
  val ittage: IttageMeta  = new IttageMeta
  ...
}
// Source: bpu/Bpu.scala:401
s3_resolveMeta.tage := RegEnable(tage.io.meta, s2_fire)
```

메타 구조:

```scala
// Source: bpu/tage/Bundles.scala:117-119
class TageMeta(implicit p: Parameters) extends TageBundle {
  val entries: Vec[TageMetaEntry] = Vec(NumBtbResultEntries, new TageMetaEntry)
}
```

| Field | Width | Description |
|--|--|--|
| `entries[i].useProvider` | 1 bit | i번째 branch에 provider 사용 여부 |
| `entries[i].providerTableIdx` | 3 bit | provider table 인덱스 |
| `entries[i].providerWayIdx` | 2 bit | provider way 인덱스 |
| `entries[i].providerTakenCtr` | 3 bit | 예측 시점 provider counter |
| `entries[i].providerUsefulCtr` | 2 bit | 예측 시점 useful counter |
| `entries[i].altOrBasePred` | 1 bit | alt or base prediction |

### t2 Update 및 Allocate 정책

- **needUpdateProvider**: hit 했고 `notNeedUpdate`가 아닌 경우 → takenCtr 업데이트
- **needUpdateAlt**: useAlt이고 `notNeedUpdate`가 아닌 경우 → alt takenCtr 업데이트
- **useAltOnNa 업데이트**: provider가 weak counter이고 alt가 맞으면 increment, 틀리면 decrement
- **Allocate**: mispredict && (finalPred != actualTaken) && provider가 최고 테이블이 아닐 때
  - 새 entry: provider보다 긴 history table 중 `!entry.valid || (takenCtr.isWeak && usefulCtr.isSaturateNegative)` 인 것 선택
  - 선택 실패 시 `usefulResetCtr` 증가 → 포화 시 전체 usefulCtrs reset

### Write Port / Conflict 처리

```scala
// Source: bpu/tage/TageTable.scala:77-86
private val entryWriteBuffers =
  Seq.tabulate(NumBanks) { bankIdx =>
    Module(new WriteBuffer(
      new EntrySramWriteReq,
      WriteBufferSize,  // = 4
      numPorts = NumWays,  // = 2
      ...
    ))
  }
// Source: bpu/tage/TageTable.scala:117
val valid = readPort.valid && !way.io.r.req.ready  // write only when SRAM read not used
```

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|--|--|--|--|
| `t0_fire` (resolve) | `TageMeta` (per branch: providerTableIdx, WayIdx, TakenCtr, UsefulCtr, altOrBasePred) | `BpuResolveMeta.tage` (s3 RegEnable) | WriteBuffer(size=4, 2 write ports per bank); SRAM single-port: write blocked when read active; bank conflict 시 `trainReady=false` stall |

---

## 품질 체크리스트

- [x] 1.1~1.8 순서 준수
- [x] memory depth/width/tables/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함 (TageEntry, TageMetaEntry)
- [x] field별 width/description 표 완성
- [x] pair BTB (mBTB) 결합 규칙 명시 (position XOR tag, altOrBasePred)
- [x] paired BTB 포함 pseudocode 작성
- [x] latency/throughput 수치화 (2 cycle s0→s2, 1 pred-block/cycle)
- [x] stage 입력/출력 타이밍 명시 (s0 SRAM req → s1 resp → s2 output)
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
