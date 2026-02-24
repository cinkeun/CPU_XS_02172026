# abtb (AheadBtb) 분석

> 분석 기준: BTB_analysis_rule.md
> 분석 대상: `src/main/scala/xiangshan/frontend/bpu/abtb/`
> Code-based only — 사전 지식/web-search 사용 금지

---

## 1.1 BTB 종류 및 역할

AheadBtb는 **Block-type BTB**로, fetch block의 taken branch 후보(way들)를 예측한다.
핵심은 "tag compare가 빠르다"가 아니라, **SRAM read + compare/select를 2-stage로 파이프라인**한다는 점이다.

ABTB 내부 타이밍(PC_A -> PC_B 관점):
1. Cycle N (abtb s0): `set/bank = f(PC_A)`로 SRAM read 요청
2. Cycle N+1 (abtb s1): SRAM 응답(`entries@PC_A`) 수신, 동시에 `io.startPc`(=PC_B)를 s2용으로 래치
3. Cycle N+2 (abtb s2): `tag = getTag(PC_B)`와 `entries@PC_A`를 비교해 hit/select 후 예측 출력

즉, **tag matching 자체는 s2 조합 1 cycle**이지만, 그 전에 **SRAM read 1 cycle**이 필요해서
ABTB 내부 latency는 2 cycle(s0->s1->s2)이다.
`ahead`의 의미는 latency를 1로 줄이는 것이 아니라, read 단계를 한 사이클 앞에 겹쳐서 **throughput 1/cycle**을 맞추는 것이다.

예측 거리(기준 PC를 명시해야 혼동이 없다):
- **BPU s0 입력 PC(PC_A) 기준**: `PC_A -> PC_B -> PC_C`로 이어지는 **two-block-ahead 성격**
- **BPU s1의 현재 block(PC_B) 기준**: `PC_B -> PC_C` 예측이므로 **non-lookahead**

즉, "non-lookahead"는 s1의 current-block 기준에서만 성립한다.

```
// Source: abtb/AheadBtb.scala:106-116 (s0 stage)
private val s0_previousStartPc = io.startPc  // read 주소를 만들 PC (앞단 block 기준)
private val s0_setIdx   = getSetIndex(s0_previousStartPc)
private val s0_bankIdx  = getBankIndex(s0_previousStartPc)
// → SRAM read를 s0에서 s0_previousStartPc로 시작

// Source: abtb/AheadBtb.scala:124 (s1 stage)
private val s1_startPc = io.startPc  // 현재 cycle의 live PC (s2에서 tag 비교용)
// → tag 비교는 s2에서 s2_startPc(= RegEnable(s1_startPc))로 수행
```

| BTB  | Type   | Block Width            | Predict Distance              | 설명 |
|------|--------|------------------------|-------------------------------|------|
| abtb | Block  | FetchBlockSize (bytes) | s0(PC_A) 기준: two-block-ahead / s1(PC_B) 기준: 현재 block → 다음 block | 1024-entry, SRAM-based, lookahead-read |

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

### TakenCounter (레지스터, AheadBtb top-level)

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
| SRAM (entry)   | 32 sets × 8 ways = 256     | AheadBtbEntry 크기       | 4     | 1/bank (single-port, read priority) | 1/bank (write buffer로 큐잉, size=4) |
| takenCounter   | 4 banks × 32 sets × 8 ways | TakenCounterWidth = 2-bit | 4 (Reg array 차원) | 전체 병렬 read    | 조건부 update (t1에서) |

- NumEntries = 1024, NumBanks = 4, NumWays = 8, NumSets = 1024/8/4 = **32**
- SRAM single-port → read와 write가 동시에 오면 write buffer(size=4)에 큐잉, read 우선

---

## 1.3 BTB memory entry 설명

```scala
// Source: abtb/Bundles.scala:83-91
class AheadBtbEntry(implicit p: Parameters) extends AheadBtbBundle {
  val valid:           Bool            = Bool()
  val tag:             UInt            = UInt(TagWidth.W)           // 24-bit partial tag
  val position:        UInt            = UInt(CfiPositionWidth.W)   // fetch block 내 branch 위치
  val attribute:       BranchAttribute = new BranchAttribute
  val targetLowerBits: UInt            = UInt(TargetLowerBitsWidth.W) // 22-bit partial target
  val targetCarry: Option[TargetCarry] = if (EnableTargetFix) Option(new TargetCarry) else None
}
```

| Field Name        | Width (bit)                | Description |
|-------------------|----------------------------|-------------|
| valid             | 1                          | entry 유효 여부 |
| tag               | 24 (TagWidth)              | PC[instOffsetBits + TagWidth - 1 : instOffsetBits] (bankIdx/setIdx 포함 범위) |
| position          | CfiPositionWidth           | fetch block 내 branch 위치 |
| attribute         | 4                          | BranchAttribute (branchType 2-bit + rasAction 2-bit) |
| targetLowerBits   | 22 (TargetLowerBitsWidth)  | target 하위 비트 (2B-aligned) |
| targetCarry       | 2 (opt)                    | EnableTargetFix=false 기본 → 미포함 |

### Tag 구조 (lookahead-read의 핵심)

```scala
// Source: abtb/Helpers.scala:24-35
val addrFields = AddrField(
  Seq(
    ("instOffset", instOffsetBits),  // PC 하위 비트 (block 내 반 워드 오프셋)
    ("bankIdx",    BankIdxWidth),    // → SRAM read index (s0_previousStartPc 사용)
    ("setIdx",     SetIdxWidth)      // → SRAM read index (s0_previousStartPc 사용)
  ),
  extraFields = Seq(
    ("tag",         instOffsetBits, TagWidth),           // → tag 비교 (s1_startPc 사용)
    ("targetLower", instOffsetBits, TargetLowerBitsWidth)
  )
)
// tag는 instOffsetBits부터 시작 → bankIdx/setIdx 필드와 겹치는 범위 포함
// 즉, 이전 PC의 bank/set으로 읽고, 현재 PC의 tag로 비교하는 구조
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

| BTB  | Paired Unit       | 역할                               | 결합 방식 |
|------|-------------------|------------------------------------|-----------|
| abtb | MicroTage (utage) | 조건부 branch 방향 결정 (override)  | abtb entry hit + position 일치 시 utage.taken 사용 |
| abtb | MicroRas (uras)   | return address 제공                | s1_prediction.attribute.isReturn이면 uras.retTarget 사용 |

- abtb 8 way + ubtb 1 way = 총 9개 s1_btbPrediction 슬롯 중 첫 번째 taken branch 선택
- 조건부 branch의 방향 최종 결정: utage hit 여부에 따라 utage.taken 또는 abtb.taken

---

## 1.5 다음 예측 pseudocode

```text
onPredict(PC_A, PC_B):
  // --- abtb s0 ---
  // PC_A로 read를 "먼저" 걸어 둔다.
  bank.readReq(setIdx = getSetIndex(PC_A),
               bankMask = UIntToOH(getBankIndex(PC_A)))

  // --- abtb s1 ---
  // 직전 cycle read 응답은 PC_A 기반 row.
  // 동시에 현재 예측 대상 PC_B를 s2용으로 래치.
  s1_startPc = io.startPc   // = PC_B
  entries = bank.readResp() // entries@PC_A

  // --- abtb s2 ---
  s2_startPc = RegEnable(s1_startPc, s1_fire)   // = PC_B
  s2_entries = RegEnable(entries, s1_fire)
  s2_tag = getTag(s2_startPc)                   // getTag(PC_B)
  s2_hitMask[i] = s2_entries[i].valid && s2_entries[i].tag === s2_tag

  // 다중 hit(같은 position) 감지 → multi-hit 발생 시 하나를 무효화
  if detectMultiHit(s2_hitMask, s2_entries.map(_.position)):
    bank.writeInvalidate(multiHitWayIdx)

  // taken counter로 방향 결정
  s2_ctrResult[i] = takenCounter[bankIdx][setIdx][i].isPositive

  for i in 0..NumWays-1:
    prediction[i].valid       = s2_valid && s2_hitMask[i]
    prediction[i].taken       = s2_ctrResult[i]
    prediction[i].cfiPosition = s2_entries[i].position
    prediction[i].attribute   = s2_entries[i].attribute
    prediction[i].target      = getFullTarget(s2_startPc, s2_entries[i].targetLowerBits)

  // BPU top에서 utage 방향 override 가능 (조건부 branch에 한함)
  for i in 0..8 (ubtb+abtb):
    if utage.hit && position match:
      s1_takenMask[i].taken = utage.taken
```

---

## 1.6 Input-to-output latency 및 throughput

| BTB  | Input Stage  | Output Stage        | Latency (cycle) | Throughput (pred/cycle) |
|------|--------------|---------------------|-----------------|--------------------------|
| abtb | abtb s0 (BPU s0) | abtb s2 (BPU s1 출력) | 2 (내부 s0→s1→s2) | 1 |

- 내부 파이프라인: s0 (SRAM read req) → s1 (entries 도착, s1_startPc 래치) → s2 (tag 비교, 출력)
- BPU 관점에서는 s1 시점에 예측 결과가 유효 (s2_valid = true after s1_fire)
- predictionSent = io.stageCtrl.s1_fire (BPU s1_fire) → abtb s2_fire trigger

```scala
// Source: abtb/AheadBtb.scala:78-99
s0_fire := io.enable && predictReqValid          // predictReqValid = io.stageCtrl.s0_fire
s1_fire := io.enable && s1_valid && s2_ready && predictReqValid
s2_fire := io.enable && s2_valid && predictionSent  // predictionSent = io.stageCtrl.s1_fire
```

---

## 1.7 Pipeline stage 위치

| Signal                 | Produced @ Stage         | Consumed @ Stage   | Timing Note |
|------------------------|--------------------------|--------------------|-------------|
| s0_previousStartPc     | BPU s0 (io.startPc)      | abtb s0            | bank.readReq에 직결 |
| bank.readResp.entries  | abtb s1 (SRAM latency 1) | abtb s1            | s1_entries = Mux1H(bankMask, ...) |
| s1_startPc             | abtb s1 (새 io.startPc)  | abtb s2            | `RegEnable(s1_startPc, s1_fire)` |
| s2_entries             | abtb s2                  | abtb s2            | `RegEnable(entries, s1_fire)` |
| io.prediction[0..7]    | abtb s2                  | BPU s1             | s2_valid = true |
| io.meta                | abtb s2                  | BPU s2→s3 (fastTrain 용) | `s2_abtbMeta = RegEnable(abtb.io.meta, s1_fire)` |

```scala
// Source: abtb/AheadBtb.scala:143-145, 148-152
private val s2_setIdx   = RegEnable(Mux(overrideValid, s3_setIdx, s1_setIdx), s1_fire)
private val s2_entries  = RegEnable(Mux(overrideValid, s3_entries, s1_entries), s1_fire)
private val s2_startPc  = RegEnable(s1_startPc, s1_fire)

// overrideValid 시 s3 (이전 s2) 값을 재사용하여 한 사이클 빠른 재예측
s2_ready := s2_fire || !s2_valid || overrideValid || redirectValid
```

- **Override 발생 시**: s3 레지스터(이전 s2 결과)를 s2로 재공급하여 즉시 재예측 지원

---

## 1.8 Training 방법

### Trigger: fast-train (s3 finalPrediction + abtbMeta)

```scala
// Source: abtb/AheadBtb.scala:204-215
private val t0_train = io.fastTrain.get.bits
private val t0_fire  = io.enable && io.fastTrain.get.valid &&
                       t0_train.finalPrediction.taken &&
                       t0_train.abtbMeta.valid
// → 조건: s3 finalPrediction이 taken이고, abtbMeta가 유효한 경우에만 train
```

```scala
// Source: bpu/Bpu.scala:182-188
fastTrain.bits.abtbMeta := s3_abtbMeta  // s2_abtbMeta를 s3로 래치한 값
// AheadBtbMeta: valid, setIdx, bankMask, entries[NumWays](hit, attribute, position, targetLowerBits)
```

### BPU 내부 meta 전달 (AheadBtbMeta)

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

### t1 train 동작

```scala
// Source: abtb/AheadBtb.scala:230-303 (simplified)
// taken counter 갱신
for each way:
  if cond && posBefore: decrease (branch before taken branch)
  if cond && posEqual:  increase (matching branch position)
  if writeResp.needResetCtr: resetWeakPositive (새 entry 할당 시)

// entry 갱신
if not hit (position+attribute 기준):
  write new entry to victim way (PLRU)
elif indirect && target mismatch:
  correct target in existing entry
```

| Trigger                                | Required FastTrain Info                    | Storage Path               | Write Port / Conflict Handling |
|----------------------------------------|--------------------------------------------|----------------------------|--------------------------------|
| s3_valid && finalPrediction.taken && abtbMeta.valid | startPc, finalPrediction (taken, position, target, attribute), abtbMeta (setIdx, bankMask, per-way hit/attr/pos/target) | `s2_abtbMeta -> s3_abtbMeta -> fastTrain` (FTQ 저장 없음) | SRAM single-port: read 우선, write는 write buffer(size=4) 큐잉 |

- AheadBtbMeta는 BPU 내부 레지스터(`s2_abtbMeta`, `s3_abtbMeta`)로만 전달됨 (FTQ에 저장 안 됨)
- write buffer full 시 write 요청이 drop됨 (`write_buffer_full_drop_write` perf counter)

---

## 1.9 Override 및 redirection

```scala
// Source: abtb/AheadBtb.scala:80-99 (abtb 내부 flush/ready 로직)
s2_flush := redirectValid            // backend redirect → abtb s2 flush
s1_flush := s2_flush
s2_ready := s2_fire || !s2_valid || overrideValid || redirectValid
// overrideValid = s3_override (BPU top에서 주입)
// → s3_override 발생 시 s2가 즉시 free되어 다음 사이클에 s3 값으로 재예측 가능

// Source: bpu/Bpu.scala:200-201
abtb.io.redirectValid := redirect.valid
abtb.io.overrideValid := s3_override
```

```scala
// Source: bpu/Bpu.scala:434-441 (BPU top next PC 선택)
s0_startPc := MuxCase(
  s0_startPcReg,
  Seq(
    redirect.valid -> redirect.bits.target,   // 최우선: backend redirect
    s3_override    -> s3_prediction.target,   // 2순위: mbtb s3 override
    s1_valid       -> s1_prediction.target    // 3순위: abtb/ubtb s1 prediction
  )
)
```

| Condition               | Winner            | Redirect Target          | Side Effect (Flush/Replay) |
|-------------------------|-------------------|--------------------------|----------------------------|
| redirect.valid          | Backend           | redirect.bits.target     | abtb s2_flush, s1_flush; BPU 전체 파이프 flush |
| s3_override             | mbtb s3 결과      | s3_prediction.target     | abtb s2_ready 즉시 free; s3 값으로 다음 사이클 재예측 |
| !redirect && !s3_override && s1_taken | uBTB+ABTB 후보 중 first taken(min position) | s1_prediction.target | 없음 |
| !redirect && !s3_override && !s1_taken | FallThrough       | s1_prediction.target     | 없음 |

- `s1_taken`과 `first taken(min position)` 선택은 BPU top(`s1_takenMask`, `CompareMatrix`, `s1_firstTakenBranch`)에서 결정됨
  (`bpu/Bpu.scala:266-304`)
- s3_override 시: abtb의 `s2_entries/s2_startPc`가 s3 값(`s3_entries/s3_setIdx/...`)으로 Mux 교체됨
  → 별도 SRAM read 없이 1사이클 만에 mbtb 기반 재예측 가능

---

## 품질 체크리스트

- [x] 1.1~1.9 순서 준수
- [x] memory depth/width/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함
- [x] field별 width/description 표 완성
- [x] pair predictor 결합 규칙 명시 (utage, uras)
- [x] pseudocode 포함
- [x] latency/throughput 수치화 (2 cycle 내부, BPU s1 출력)
- [x] stage 입력/출력 타이밍 명시
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
- [x] override/redirection 우선순위 및 근거 명시
