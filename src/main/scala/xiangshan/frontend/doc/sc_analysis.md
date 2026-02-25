# SC (Statistical Corrector) 분석

> 분석 기반: Scala/Chisel 코드 only (code-based)
> 분석 대상: `src/main/scala/xiangshan/frontend/bpu/sc/`

---

## 1.0 SC가 필요한 이유

핵심: **TAGE provider가 "맞는 방향"을 가리키더라도, 특정 (PC, history) 컨텍스트에서는 통계적으로 반대가 더 자주 맞는 경우가 존재한다.**

### TAGE provider의 구조적 한계

TAGE의 최종 예측은 provider 테이블의 saturating counter 부호이다. Counter가 `weakTaken(+1)` 또는 `weakNotTaken(-1)` 상태일 때 예측 신뢰도가 낮다. Counter가 saturate되어 있더라도 **특정 path/history 패턴 하에서는 체계적으로 틀릴 수 있다**.

### TAGE 신뢰도와 SC 개입 임계값의 반비례 관계

```scala
// Source: sc/Sc.scala:318-334
val tageConfHigh = s2_providerCtr(i).isSaturatePositive || s2_providerCtr(i).isSaturateNegative
val tageConfMid  = s2_providerCtr(i).isMid
val tageConfLow  = s2_providerCtr(i).isWeak

when(hit && valid && tageConfHigh) {
  conf := aboveThreshold(sum, thres >> 1)   // threshold ÷ 2 → 어렵게 개입
}.elsewhen(hit && valid && tageConfMid) {
  conf := aboveThreshold(sum, thres >> 2)   // threshold ÷ 4
}.elsewhen(hit && valid && tageConfLow) {
  conf := aboveThreshold(sum, thres >> 3)   // threshold ÷ 8 → 쉽게 개입
}
```

TAGE가 weak(불확실)할수록 SC 임계값이 낮아져 override가 쉬워지고, TAGE가 saturate(자신감 높음)이면 임계값이 높아져 SC가 개입하기 어렵다.

### SC가 학습하는 편향 종류

| 테이블 | 인덱스 구성 | 포착하는 편향 | 활성화 |
|---|---|---|---|
| `biasTable` | `(PC, tageProviderIsWeak, tageProviderTaken)` | TAGE 자체의 weak 예측 방향 편향 | **true** |
| `pathTable` | `(PC, path history)` | call path에 따른 분기 편향 | **true** |
| `globalTable` | `(PC, GHR)` | 전역 분기 패턴 편향 | false |
| `bwTable` | `(PC, backward history)` | 루프/역방향 패턴 편향 | false |

`biasTable` 인덱스에 `tageProviderIsWeak`과 `tageProviderTaken`이 포함된 것이 핵심:

```scala
// Source: sc/Sc.scala:287-293
private val s2_biasIdxLowBits = VecInit(s2_providerTakenMask.zip(s2_providerValid).zip(s2_providerCtr).map {
  case ((taken, valid), ctr) => Cat(valid && ctr.isWeak, valid && taken)
})
// biasIdx = Cat(wayIdx, providerIsWeak, providerTaken)
// "TAGE가 weak하게 taken으로 예측할 때 실제로는 not-taken인 빈도"를 직접 학습
```

### 자기보정 임계값

```scala
// Source: sc/Sc.scala:452-456
val scWrong = taken =/= t1_meta.scPred(branchIdx)
val shouldUpdate = writeValid && ...
  (t1_meta.tagePred(branchIdx) =/= t1_meta.scPred(branchIdx)) && // SC가 TAGE와 다를 때만
  (scWrong || !t1_meta.sumAboveThres(branchIdx))
prevThres.getUpdate(scWrong, en = shouldUpdate)
// SC가 틀리면 → threshold 증가 (더 확신 있을 때만 개입)
// SC가 맞았지만 sum이 threshold 미달이었으면 → threshold 감소 (더 자주 개입)
```

---

## 1.1 Prediction Unit 종류 및 역할

SC(Statistical Corrector)는 **TAGE 예측을 보정**하는 corrector이다.
TAGE가 conditional branch에 대해 taken/not-taken을 예측하면, SC는 여러 signed counter 테이블의 합산값(percsum)을 이용하여 TAGE 예측을 뒤집을지 결정한다.

- **단독 예측기가 아님**: TAGE의 provider 예측 + mbtb hit 결과를 입력으로 받아 보정한다.
- **처리 단위**: mbtb의 NumBtbResultEntries(= NumWay × NumAlignBanks = 4 × 2 = **8**) 개 entry를 per-way로 처리한다.
- **예측 거리**: 현재 input block의 conditional branch에 대한 direction 보정 (lookahead 없음)

| Unit | Type | CFI per Entry | Predict Distance | 설명 |
|------|------|---------------|-----------------|------|
| SC | Statistical Corrector | 1 (per way) | current block | TAGE taken/not-taken 보정 |

---

## 1.2 Prediction Unit Memory Spec

### 파라미터 기준값 (`Parameters.scala`)

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

### 계산값

- `NumWays` = `NumBtbResultEntries` = `NumWay × NumAlignBanks` = 4 × 2 = **8**
- `BiasTableNumWays` = `NumWays << BiasUseTageBitWidth` = 8 << 2 = **32**
- `ScEntry` width = `CtrWidth` = **6 bits** (signed saturating counter)

### Memory 스펙 표

| Table | Size (sets) | Width (bits) | #Tables | Banks | Read Ports | Write Ports | Enable |
|-------|------------|--------------|---------|-------|------------|-------------|--------|
| PathTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank (via WriteBuffer) | **true** |
| PathTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | **true** |
| GlobalTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| GlobalTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| BWTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| BWTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false |
| BiasTable | 128 | 6×32=192 per row | 1 | 2 | 1 per bank | 1 per bank | **true** |

> `singlePort = true`: 각 bank는 read/write 포트가 물리적으로 1개. read 우선.

---

## 1.3 Memory Entry 설명

### ScEntry 정의

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

### ScEntry 필드 표

| Field Name | Width (bits) | Description |
|-----------|-------------|-------------|
| `ctr` | 6 | Signed saturating counter. `value >= 0` → taken, `< 0` → not-taken. percsum 계산 시 `Cat(ctr, 1.U(1.W)).asSInt` = `ctr*2+1`로 확장 |

### percsum 변환

```scala
// Source: sc/Helpers.scala:61
def getPercsum(ctr: SInt): SInt = Cat(ctr, 1.U(1.W)).asSInt
// ctr 값을 2배+1로 확장해서 합산 (center bias 제거)
```

### BiasTable 인덱싱

```scala
// Source: sc/Sc.scala:287
val s2_biasIdxLowBits = Cat(valid && ctr.isWeak, valid && taken)
// [1]: providerValid && providerCtr.isWeak
// [0]: providerValid && providerTaken
// biasWayIdx = Cat(wayIdx[2:0], biasIdxLowBits[1:0])  → 5-bit index into 32 ways
```

---

## 1.4 Paired BTB 설명

SC는 단독으로 작동하지 않으며, 아래 두 유닛과 결합한다:

| Prediction Unit | Paired BTB/Predictor | Pairing 목적 | 결합 Stage/Signal |
|----------------|---------------------|-------------|------------------|
| SC | mbtb (MainBTB) | conditional branch 위치 확인 (hitMask, wayIdx) | s2 / `sc.io.mbtbResult` |
| SC | TAGE | provider 예측값 및 신뢰도(ctr) 수신, SC가 TAGE를 보정 | s2 / `sc.io.providerTakenCtrs` |

```scala
// Source: bpu/Bpu.scala:230
sc.io.mbtbResult        := mbtb.io.result
sc.io.providerTakenCtrs := tage.io.toSc.providerTakenCtrVec

// Source: bpu/Bpu.scala:327
s2_condTakenMask = ... MuxCase(e.bits.taken,
  Seq(
    useSc         -> scTaken,       // SC 보정 최우선
    p.useProvider -> p.providerPred, // TAGE provider
    p.hasAlt      -> p.altPred       // TAGE alt
  ))
```

---

## 1.5 예측 Pseudocode (paired BTB 포함)

```text
onPredict(startPc, foldedPathHist, commonHR, mbtbResult, providerTakenCtrs):

  // --- Stage s0: 테이블 인덱스 계산 + SRAM read 요청 ---
  bankMask   = getBankMask(startPc)
  pathIdx[i] = PC[high] ^ foldedHist(PathTableInfos[i].HistLen)   (for i in 0..1)
  globalIdx[i] = PC[high] ^ fold(commonHR.ghr[HistLen-1:0])       (if commonHR.valid)
  bwIdx[i]   = PC[high] ^ fold(commonHR.bw[HistLen-1:0])          (if commonHR.valid)
  biasIdx    = PC[high]

  send read req to: pathTable[i], (globalTable[i] if GlobalEnable),
                    (bwTable[i] if BWEnable), biasTable

  // --- Stage s1: 응답 수집 + percsum 누산 (way 단위) ---
  for each way w in 0..NumWays-1:
    pathPercsum[w]   = sum(getPercsum(pathResp[i][w].ctr)   for i)
    globalPercsum[w] = sum(getPercsum(globalResp[i][w].ctr) for i)  // 0 if !commonHR.valid
    bwPercsum[w]     = sum(getPercsum(bwResp[i][w].ctr)     for i)  // 0 if !commonHR.valid
    sumPercsum[w]    = pathPercsum[w] + globalPercsum[w] + bwPercsum[w]

  // --- Stage s2: mbtb/TAGE 결합 + 임계값 판정 ---
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
    scTakenMask[w] = scPred[w]          // SC의 raw direction 예측
    scUsed[w]      = useScPred[w]       // TAGE를 실제로 override할지 여부
    meta = { scPathResp, scBiasResp, scPred, tagePred, useScPred, sumAboveThres, ... }

  // aboveThreshold(sum, thres): |sum| > thres (부호 포함 체크)
```

---

## 1.6 Input-to-Output Latency 및 Throughput

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|------------|-------------|----------------|------------------------|
| SC (scTakenMask, scUsed) | s0 (SRAM req) | s2 (combinatorial 출력) | 2 | 1 block / cycle |
| SC meta | s0 | s3 (RegEnable of s2_fire) | 3 | 1 block / cycle |

> SRAM: `holdRead=true` → s1에서 응답 유효.
> s2 출력은 combinatorial (s1 결과에 mbtb/TAGE s2 신호 결합).

---

## 1.7 Pipeline Stage 위치

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--------|-----------------|-----------------|-------------|
| `s0_pathIdx`, `s0_biasIdx` | s0 | s0 (SRAM req) | combinatorial from startPc + foldedPathHist |
| `s0_globalIdx`, `s0_bwIdx` | s0 | s0 (SRAM req) | gated by `commonHR.valid` |
| `s1_pathResp`, `s1_biasResp` | s1 | s1 (percsum 계산) | SRAM 1-cycle read latency |
| `s1_sumPercsum[w]` | s1 | s2 (RegEnable s1_fire) | path+global+bw 합산 |
| `mbtbResult` input | s2 | s2 | mbtb result 동일 cycle |
| `providerTakenCtrs` input | s2 | s2 | TAGE s2 output |
| `scTakenMask`, `scUsed` | s2 | s2 (Bpu.scala 결합) | s2 combinatorial output |
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

// GHR override 시 state machine으로 최대 3 사이클 backfill
// idle → state1 → state2 → state3 → idle
```

---

## 1.8 Training 방법

### Training Trigger

- FTQ resolve → `t0_fire` (= `io.stageCtrl.t0_fire`)
- t1: `t1_train = RegEnable(io.train, t0_fire)` → 1 cycle 지연 후 처리

### Training 조건

```scala
// Source: sc/Helpers.scala:84
val needUpdate = writeValid && writeWayIdx === wayIdx &&
  metaData.tagePredValid(branchIdx) &&
  (metaData.scPred(branchIdx) =/= writeTaken || !metaData.sumAboveThres(branchIdx))
// 조건: TAGE provider가 valid이고, SC 예측이 틀렸거나 SC 합산이 임계값 이하일 때
```

### Threshold 업데이트 조건

```scala
// Source: sc/Sc.scala:452-456
val shouldUpdate = writeValid && writeWayIdx === wayIdx &&
  metaData.tagePredValid(branchIdx) &&
  (metaData.tagePred(branchIdx) =/= metaData.scPred(branchIdx)) &&
  (scWrong || !metaData.sumAboveThres(branchIdx))
prevThres.getUpdate(scWrong, en = shouldUpdate)
// scWrong=true → threshold 감소(더 자주 SC 사용)
// scWrong=false → threshold 증가(더 드물게 SC 사용)
```

### FTQ 보관 정보

```scala
// Source: sc/Bundles.scala:69
class ScMeta(implicit p: Parameters) extends ScBundle with HasScParameters {
  val scPathResp:      Vec[Vec[UInt]] // NumPathTables × NumWays × 6 bits
  val scGlobalResp:    Vec[Vec[UInt]] // NumGlobalTables × NumWays × 6 bits
  val scBWResp:        Vec[Vec[UInt]] // NumBWTables × NumWays × 6 bits
  val scBiasResp:      Vec[UInt]      // BiasTableNumWays(=32) × 6 bits
  val scBiasLowerBits: Vec[UInt]      // NumWays × 2 bits (TAGE 신뢰도 하위 비트)
  val scCommonHR:      CommonHREntry  // ghr/bw history (training 시 index 재계산용)
  val scPred:          Vec[Bool]      // NumWays: SC raw direction 예측
  val tagePred:        Vec[Bool]      // NumBtbResultEntries: TAGE provider 예측
  val tagePredValid:   Vec[Bool]      // NumBtbResultEntries: TAGE provider valid
  val useScPred:       Vec[Bool]      // NumWays: SC 실제 override 여부
  val sumAboveThres:   Vec[Bool]      // NumWays: 합산이 임계값 초과 여부
}
```

### Training 흐름 요약

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|---------|-------------------|-------------|-------------------------------|
| FTQ resolve (t0_fire) | scPathResp, scBiasResp, scCommonHR, scPred, tagePred, tagePredValid, useScPred, sumAboveThres | `io.train.meta.sc` (ScMeta) | WriteBuffer(size=4) per bank. read 우선: `bank.io.w.req.valid := buffer.valid && !bank.io.r.req.valid` |

### WriteBuffer 동작

```scala
// Source: sc/ScTable.scala:108-113
sram.zip(writeBuffer).foreach { case (bank, buffer) =>
  bank.io.w.req.valid  := buffer.io.read.head.valid && !bank.io.r.req.valid
  // read가 없을 때만 write. read 충돌 시 WriteBuffer에서 대기
  buffer.io.read.head.ready := bank.io.w.req.ready && !bank.io.r.req.valid
}
```

- **write 우선순위**: read 요청이 없을 때만 write 수행 → read-first
- **충돌 처리**: WriteBuffer(FIFO, depth=4)가 pending write 보관
- **wayMask 기반 selective write**: 변경된 way만 `wayMask`로 선택적 업데이트

---

## 품질 체크리스트

- [x] 1.1~1.8 순서 준수
- [x] memory depth/width/tables/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함
- [x] field별 width/description 표 완성
- [x] pair BTB 결합 규칙 명시
- [x] paired BTB 포함 pseudocode 작성
- [x] latency/throughput 수치화
- [x] stage 입력/출력 타이밍 명시
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
