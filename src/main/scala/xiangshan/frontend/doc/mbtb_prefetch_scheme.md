# mBTB + TAGE Pre-fetch Buffer Scheme for Frontend IPC Enhancement

## 1. 배경 및 문제

### 현재 예측 구조

XiangShan frontend BPU는 fast path와 slow/high-accuracy path가 pipeline stage별로 결합된 구조이다.

| Stage | 주요 구성 | 역할 |
|-------|-----------|------|
| S1 | uBTB + ABTB + uTAGE + uRAS | 빠른 next fetch PC 생성 |
| S2 | mBTB + TAGE + SC | 더 큰 BTB와 history 기반 direction 보정 |
| S3 | latched mBTB/TAGE/SC + ITTAGE + RAS | indirect/return target 보정 및 최종 prediction 조립 |

현재 S1 prediction은 FTQ로 먼저 전달되고, S3 final prediction이 S1 prediction과 다르면 `s3_override`가 발생한다.

### Override 문제

- S1 결과와 S3 결과가 다를 경우 `s3_override`가 발생한다.
- override가 발생하면 BPU 내부 S1/S2 in-flight prediction이 flush되고, FTQ/IFU/prefetch pointer가 필요 시 rollback된다.
- 반복적인 override는 frontend bandwidth 저하와 pipeline bubble 증가로 이어질 수 있다.

이 문서의 목표는 S3 final prediction 전체를 앞당기는 것이 아니라, **mBTB + TAGE 수준의 prediction을 미리 준비해서 S1 prediction 품질을 높이고 S3 override 발생률을 낮추는 것**이다.

---

## 2. 제안 아이디어: mBTB + TAGE Pre-fetch Buffer Scheme

### 핵심 아이디어

uBTB entry에 현재 fetch block 기준 **3-block ahead PC**와 TAGE folded-history delta를 추가로 저장한다.
S1에서 uBTB entry를 읽을 때 이 metadata를 이용해 `PC_{X+3}`에 대한 mBTB + TAGE prefetch를 opportunistic하게 발행하고, 결과를 32-entry CAM buffer에 저장한다.

3 fetch block 뒤 실제 fetch가 `PC_{X+3}`에 도달했을 때 buffer가 hit하면, 기존 S1 prediction 대신 precomputed mBTB + TAGE result를 사용한다.

**4-ahead 대비 3-ahead를 선택한 이유**: PC_X → PC_{X+3} 사이 intermediate branch 수가 하나 줄어들어 historyDelta 정확도가 높아지고, speculative folded history 오류 가능성이 감소한다.

중요한 제한:

- prefetch 대상은 **mBTB + TAGE까지만**이다.
- SC, ITTAGE, RAS는 prefetch path에서 제외한다.
- S3 override는 항상 기존처럼 허용한다.
- 따라서 이 scheme은 correctness를 위해 S3 final path를 억제하지 않는다.
- 기대 효과는 S1 prediction이 더 정확해져서 S3 override가 자연스럽게 줄어드는 것이다.

### 타이밍 분석

```
Cycle N:   PC_X → S0

Cycle N+1: PC_X → S1
           uBTB hit → aheadPc = PC_{X+3}, foldedHistoryDelta 획득
           speculative folded history 생성

Cycle N+2: PC_{X+1} → S0 (regular)
           PC_{X+3} prefetch request → mBTB S0 (sideband)
           mBTB S0 타이밍에 맞춰 발행: regular read 우선, conflict 시 drop

Cycle N+3: PC_{X+2} → S1 (regular)
           PC_{X+3} prefetch → mBTB S1 (wait)

Cycle N+4: PC_{X+3} → S1 (regular fetch 도달)
           PC_{X+3} prefetch → mBTB S2 (result 완료) → buffer write
           ─────────────────────────────────────────────────
           buffer write와 buffer read(CAM lookup)가 같은 사이클 발생
           → same-cycle write-read bypass 필수
           ─────────────────────────────────────────────────
           CAM HIT  → S1 prediction을 buffer result로 대체
           CAM MISS → 기존 uBTB/ABTB/uTAGE path 사용
```

**Bypass 설계**: write key = `{PC_{X+3}, historySignature}`, read key = `{s1_startPc, expectedSignature}`.
같은 cycle에 write/read key가 동일하면 CAM array에 기록 완료 전이라도 write data를 read result로 forward한다.
TAGE prefetch도 동일 타이밍(mBTB S0 사이드밴드 발행, S2 결과)으로 맞춰야 한다.

**3-ahead 타이밍 특성**: prefetch 완료(cycle N+4)와 fetch의 S1 도달(cycle N+4)이 같은 사이클이므로 bypass는 필수이며 대안이 없다. S0 lookup / S1 use 구조는 S0 of PC_{X+3}이 cycle N+3이지만 prefetch 완료가 N+4이므로 3-ahead에서는 불가능하다 — 이 구조는 4-ahead(완료 N+4, S0 lookup N+4, S1 use N+5)로 전환해야만 bypass 부담을 S0 레벨로 옮길 수 있다. 3-ahead를 선택하는 한 S1 bypass는 설계 요구사항이다. 이 대신 4-ahead 대비 historyDelta 경로가 한 block 짧아져 speculative history 정확도가 향상된다는 이점이 있다.

### 구성 요소

#### (1) Pre-fetch Buffer

- 구조: 32-entry fully-associative CAM
- Hit key: `{pcTag, historySignature}`
  - `pcTag`: prefetch 대상 `PC_{X+3}`
  - `historySignature` 매칭 방식:
    - **write key**: prefetch 발행 시(cycle N+2) `phr.io.prefetchHistorySignature` = `hash(s0_foldedPhr XOR foldedHistoryDelta)`를 N+4까지 파이프라인
    - **read key**: S1 of PC_{X+3}(cycle N+4) 시점의 `hash(s1_foldedPhr)` — 실제 PHR 상태
    - 알고리즘 핵심: speculation이 맞으면 두 값 일치 → hit. redirect 등으로 실제 history가 달라지면 불일치 → miss → 기존 uBTB path fallback
- Data:
  - `taken`
  - `target`
  - `cfiPosition`
  - `attribute`
- Replacement: PLRU 또는 round-robin
- Read timing: S1 prediction selection과 같은 cycle
- Write timing: mBTB + TAGE prefetch pipeline 완료 cycle
- Same-cycle write-read bypass 지원
- Redirect 시 flush 불필요: historySignature 불일치로 자동 miss 처리됨. S3 override가 correctness 보장.

#### (2) uBTB Entry 확장

기존 uBTB entry는 현재 fetch block의 branch position/attribute/target 중심 정보를 저장한다.
이 scheme에서는 uBTB가 mBTB에는 없는 **successor/path metadata**를 추가로 가진다.

추가 필드:

```scala
val aheadValid: Bool
val aheadPc:    PrunedAddr // full/pruned fetch-block PC
val foldedHistoryDelta: Vec[...] // TAGE folded history 보정용 delta
val historySignature: UInt       // buffer hit guard용 짧은 signature
```

`aheadPc`는 partial target encoding이 아니라 full/pruned PC로 저장한다. mBTB/TAGE prefetch request와 CAM tag compare에 직접 사용하기 위해서이다.

#### (3) Folded History Delta

TAGE는 `hash(PC, folded history)` 기반으로 table index/tag를 만든다.
따라서 `PC_{X+3}`를 미리 예측하려면 `PC_{X+3}` 시점의 folded history가 필요하다.

단순 taken/NT bit만 저장하는 방식은 충분하지 않다. 현재 PHR update는 branch target과 CFI PC를 포함하는 path hash 영향을 받기 때문이다.

따라서 이 scheme의 `foldedHistoryDelta`는 다음 의미를 가진다:

- `PC_X -> PC_{X+3}` 사이 committed path의 branch들이 TAGE folded history에 미치는 영향을 precomputed delta로 저장
- runtime에는 현재 `s0_foldedPhr`에 이 delta를 적용해 `PC_{X+3}`용 speculative folded history를 생성
- raw GHR 전체를 복원하지 않고, TAGE table별 folded history를 보정하는 방식

**Packed UInt Encoding**:

TAGE 테이블 수를 N, 테이블 i의 folded history width를 `fh_w_i`라 하면:

```
FoldedHistoryDeltaWidth = sum(fh_w_i for i in 0..N-1)

foldedHistoryDelta = [ delta_{N-1} | ... | delta_1 | delta_0 ]   // LSB = table 0
```

각 `delta_i`는 commit-time에 계산한 XOR mask이다:

```
delta_i = foldedHistory_at_X[i] XOR foldedHistory_at_{X+3}[i]
```

runtime apply:

```
prefetchFoldedPhr[i] = s0_foldedPhr[i] XOR delta_i
```

folded history update가 XOR 기반이므로, delta 자체도 XOR mask로 표현된다.
테이블별 slice 범위는 `AllFoldedHistoryInfo`에서 정적으로 결정된다.

`historySignature`는 이 speculative folded history에서 만든 짧은 hash/signature이다. buffer hit 조건에 포함해 stale/wrong-path prefetch 사용을 줄인다.

#### (4) Commit-based 3-ahead Training

`aheadPc`와 `foldedHistoryDelta`는 commit된 path 기준으로 학습한다.

이유:

- 이 metadata는 mBTB가 원래 갖고 있는 branch entry가 아니다.
- uBTB entry에 “현재 block에서 3 fetch-block 뒤로 이어지는 committed successor path”를 저장하는 것이다.
- wrong-path resolve 결과로 학습하면 uBTB에 잘못된 3-ahead path를 심을 수 있다.

학습 시 필요한 정보:

- `startPc = PC_X`
- `aheadPc = PC_{X+3}`
- `foldedHistoryDelta`
- `historySignature`

FTQ commit side에서 `commitPtr ~ commitPtr+3` window를 추적하고, 해당 window가 모두 committed 되었을 때 uBTB 3-ahead metadata를 갱신한다.

### 동작 흐름 요약

```
[Prefetch Issue]
S1 uBTB hit for PC_X
  ├─ aheadValid 확인
  ├─ aheadPc = PC_{X+3}
  ├─ foldedHistoryDelta로 speculative folded history 생성
  └─ mBTB + TAGE prefetch read 시도
       ├─ bank conflict 없음 → issue
       └─ bank conflict 있음 → drop

[Buffer Fill]
mBTB + TAGE prefetch result 완료
  ├─ indirect/return이면 buffer write 제외
  ├─ taken=true  → branch prediction 저장
  └─ taken=false → fallThrough-style prediction 저장

[Buffer Use]
S1 fetch PC_{X+3}
  ├─ CAM lookup key = {PC_{X+3}, expectedHistorySignature}
  ├─ HIT  → S1 prediction을 buffer result로 대체
  └─ MISS → 기존 uBTB/ABTB/uTAGE result 사용

[S3 Check]
기존 S3 final path는 항상 유지
  ├─ SC flip 가능
  ├─ ITTAGE/RAS target correction 가능
  └─ S3 prediction != S1 prediction이면 기존처럼 s3_override 발생
```

### Prediction 사용 정책

#### Buffer Hit Priority

buffer hit 시 prefetch result가 기존 S1 result보다 우선한다.

```scala
when(prefetchBufferHit) {
  s1_prediction := prefetchBufferPrediction
}.otherwise {
  s1_prediction := originalS1Prediction
}
```

단, `s3_override`는 절대 suppress하지 않는다.

#### Indirect / Return 처리

prefetch result는 conditional/direct branch에만 사용한다.

- conditional: mBTB candidate + TAGE direction 반영
- direct: mBTB target 사용
- indirect: 사용하지 않음
- return: 사용하지 않음

indirect/return은 ITTAGE/RAS 의존성이 크므로 S3 final path에 맡긴다.

#### Not-taken 처리

`taken=false` result도 buffer에 저장하고 사용한다.

단, not-taken result는 branch identity를 유지하지 않고 fallThrough-style prediction으로 저장한다.

- `taken = false`
- `attribute = None`
- `cfiPosition = fallThrough.cfiPosition`
- `target = fallThrough.target`

이 방식은 기존 `Prediction ===` 비교와 잘 맞고, uBTB/ABTB가 taken으로 예측했지만 mBTB+TAGE가 not-taken으로 판단하는 케이스를 앞당길 수 있다.

### 기대 효과

- mBTB/TAGE 기반 taken/not-taken mismatch를 S1에서 일부 제거
- mBTB가 더 정확히 찾은 earlier cfiPosition을 S1에서 먼저 사용 가능
- direct branch target mismatch 일부 감소
- S3 override 빈도 감소 기대

### 남는 override

다음 케이스는 여전히 S3 override가 발생할 수 있다.

- SC가 TAGE direction을 flip하는 경우
- ITTAGE가 indirect target을 보정하는 경우
- RAS가 return target을 보정하는 경우
- buffer result가 stale이거나 historySignature는 통과했지만 실제 S3 result와 다른 경우
- prefetch가 bank conflict로 drop되어 buffer miss가 난 경우

### 리스크

- S1 critical path에 32-entry CAM compare와 prediction mux가 추가된다.
- uBTB entry width가 증가한다.
- mBTB/TAGE banking 확장이 area와 timing에 영향을 줄 수 있다.
- prefetch는 conflict 시 drop하므로 coverage가 bank conflict rate에 민감하다.
- foldedHistoryDelta 생성과 commit window tracking이 FTQ commit side 복잡도를 높인다.
- **indirect/return 제외로 인한 효과 제한**: s3_override 중 ITTAGE/RAS correction이 차지하는 비율이 높은 workload (예: C++ virtual dispatch, recursive call 중심 코드)에서는 이 scheme의 실효 override 감소폭이 미미할 수 있다. 구현 전 workload별 override 원인 분포를 simulation으로 먼저 측정해야 scheme 투자 대비 효과를 예측할 수 있다.

---

## 3. 관련 논문 비교

### 3.1 LLBP — MICRO 2024

**"The Last-Level Branch Predictor"**  
David Schall, Andreas Sandberg, Boris Grot (University of Edinburgh)

- [ACM DL](https://dl.acm.org/doi/10.1109/MICRO61859.2024.00042)
- [GitHub](https://github.com/dhschall/LLBP)

#### 논문 방식

- TAGE predictor를 fast predictor로 두고, 대용량 LLBP storage를 backing store로 운용
- Branch program context를 기준으로 prediction pattern을 LLBP에 저장
- 4 unconditional branches ahead를 lookahead distance로 삼아 LLBP 결과를 미리 prefetch
- 소형 in-core PatternBuffer에 저장 후 TAGE와 병렬 접근
- PatternBuffer hit 시 LLBP 결과 사용, miss 시 TAGE 결과만 사용

#### 유사점

| 항목 | LLBP | 제안 scheme |
|------|------|-------------|
| 계층 구조 | TAGE fast + LLBP slow | S1 fast path + mBTB/TAGE prefetch |
| Prefetch 방식 | N branches ahead | 4 fetch blocks ahead |
| Buffer 위치 | PatternBuffer | mBTB/TAGE prefetch buffer |
| Hit guard | context 기반 | PC tag + historySignature |
| 목적 | slow predictor latency hiding | mBTB/TAGE result를 S1까지 당김 |

#### 차이점

| 항목 | LLBP | 제안 scheme |
|------|------|-------------|
| 목적 | Prediction accuracy 향상 | S3 override 빈도 감소 |
| Prefetch 단위 | branch/context pattern | fetch block PC |
| Backing store | 별도 LLBP | 기존 mBTB/TAGE |
| 구현 정책 | dedicated backing store access | conflict-free opportunistic access |
| Final override | TAGE와 병렬 사용 | S3 final path는 항상 유지 |

---

### 3.2 Two Level Bulk Preload — HPCA 2013

**"Two Level Bulk Preload Branch Prediction"**  
Bonanno, Collura 외 (IBM, zEnterprise EC12)

- [HPCA 2013 PDF](https://class.ece.iastate.edu/tyagi/cpre581/papers/HPCA13BulkPreloadBranch.pdf)
- [IEEE Xplore](https://ieeexplore.ieee.org/document/6522308/)

#### 논문 방식

- BTB1 + BTB2 2-level 구조
- BTB1 miss 감지 시 BTB2에서 bulk preload
- BTB2 접근을 제한해 power 효율 개선
- hit 가능성이 높은 entry를 preload buffer에 준비

#### 유사점

| 항목 | HPCA 2013 | 제안 scheme |
|------|-----------|-------------|
| 계층 구조 | BTB1 + BTB2 | S1 BTB + mBTB |
| 중간 buffer | BTB preload buffer | mBTB/TAGE prefetch buffer |
| 목적 | redirect latency 감소 | S3 override 감소 |
| 접근 정책 | lower-level BTB preload | mBTB/TAGE opportunistic prefetch |

#### 차이점

| 항목 | HPCA 2013 | 제안 scheme |
|------|-----------|-------------|
| Prefetch trigger | BTB1 miss 기반 reactive | uBTB threeAhead 기반 proactive |
| Prefetch 대상 | BTB entry | mBTB + TAGE prediction result |
| History 처리 | BTB 중심 | folded history delta 필요 |
| Conflict policy | 논문 구조 의존 | regular read 우선, conflict 시 drop |

---

### 3.3 Branch Pre-Prediction — KCI 2009

**"Branch Prediction Latency Hiding Scheme using Branch Pre-Prediction and Modified BTB"**

- [KCI Journal](https://journal.kci.go.kr/jksci/archive/articleView?artiId=ART001388533)

#### 유사점

| 항목 | KCI 2009 | 제안 scheme |
|------|----------|-------------|
| 핵심 방향 | fetch 이전에 prediction 준비 | mBTB/TAGE 결과를 fetch 이전에 준비 |
| BTB 변경 | Modified BTB | uBTB threeAhead metadata |
| 목적 | prediction latency hiding | slow-path result를 S1에서 사용 |

#### 차이점

| 항목 | KCI 2009 | 제안 scheme |
|------|----------|-------------|
| 대상 latency | 단일 predictor latency | mBTB/TAGE slow-path latency |
| Decoupling | predictor/fetch decoupling | prefetch buffer 기반 |
| Final correction | 구조별 상이 | S3 final override 항상 유지 |

---

## 4. 종합 비교

| 논문 | 메커니즘 유사도 | 목적 유사도 | 참고 우선순위 |
|------|----------------|------------|--------------|
| LLBP (MICRO 2024) | 높음 | 중간 | 1순위: lookahead + buffer + history guard 참고 |
| HPCA 2013 | 중간 | 높음 | 2순위: two-level BTB preload 관점 참고 |
| KCI 2009 | 중간 | 중간 | 3순위: latency hiding 방향성 참고 |

제안 scheme은 LLBP의 lookahead/buffer 개념을 XiangShan의 mBTB/TAGE slow path에 맞게 적용하되, dedicated read port를 추가하지 않고 banking 확장과 conflict-free opportunistic issue를 사용한다.

---

## 5. Chisel 아키텍처 변경 사항

이 section은 Chisel RTL 기준 변경 사항만 다룬다. gem5 모델은 별도 코드 확인 후 기능 등가 수준으로 다시 정의해야 하며, 여기서는 구체 구현 파일이나 클래스명을 가정하지 않는다.

### 5.0 Scope Decision

구현 범위:

- 포함: mBTB + TAGE prefetch
- 제외: SC, ITTAGE, RAS prefetch
- S1에서 buffer hit 시 prefetch result로 prediction 대체
- S3 final path는 항상 유지
- `s3_override`는 suppress하지 않음

즉, 이 구현은 correctness-preserving optimization이다. buffer result가 틀려도 기존 S3 override가 최종 보정한다.

---

### 5.1 `bpu/ubtb/Parameters.scala` — MicroBtbParameters

추가 파라미터:

```scala
case class MicroBtbParameters(
  ...
  EnableThreeAheadPrefetch: Boolean = false,
  HistorySignatureWidth: Int = 12,
  FoldedHistoryDeltaWidth: Int = ...
)
```

`FoldedHistoryDeltaWidth`는 TAGE table별 folded history delta 표현 방식에 따라 결정한다.

---

### 5.2 `bpu/ubtb/Bundles.scala` — MicroBtbEntry

`MicroBtbEntry`에 3-ahead metadata 추가:

```scala
val aheadValid: Bool = Bool()
val aheadPc:    PrunedAddr = PrunedAddr(VAddrBits)

// TAGE folded history를 PC_{X+3} 기준으로 보정하기 위한 precomputed delta
val foldedHistoryDelta: UInt = UInt(FoldedHistoryDeltaWidth.W)

// prefetch buffer hit guard
val historySignature: UInt = UInt(HistorySignatureWidth.W)
```

주의:

- `aheadPc`는 partial target이 아니라 full/pruned PC이다.
- uBTB entry width 증가가 크므로 area 평가가 필요하다.

---

### 5.3 `bpu/ubtb/MicroBtb.scala` — threeAhead Metadata 경로

Predict side:

- uBTB hit entry에서 `aheadValid`, `aheadPc`, `foldedHistoryDelta`, `historySignature`를 출력한다.
- 기존 `io.prediction`과 별도 출력으로 두는 것이 좋다.

Train side:

- 기존 `fastTrain`은 slot1 prediction 학습을 유지한다.
- 3-ahead metadata는 별도 commit-based train channel로 갱신한다.

예시 IO:

```scala
val aheadInfo: Valid[UbtbAheadInfo] = Output(Valid(new UbtbAheadInfo))
val aheadTrain: Valid[UbtbAheadTrain] = Input(Valid(new UbtbAheadTrain))
```

---

### 5.4 신규 모듈 — `bpu/mbtb/MbtbTagePrefetchBuffer.scala`

32-entry fully-associative CAM buffer를 추가한다.

Entry:

```scala
class MbtbTagePrefetchBufferEntry extends Bundle {
  val valid: Bool = Bool()

  val pcTag: UInt = UInt(VAddrBits.W)
  val historySignature: UInt = UInt(HistorySignatureWidth.W)

  val taken: Bool = Bool()
  val target: PrunedAddr = PrunedAddr(VAddrBits)
  val cfiPosition: UInt = UInt(CfiPositionWidth.W)
  val attribute: BranchAttribute = new BranchAttribute
}
```

Read:

```scala
val readPc: PrunedAddr
val readHistorySignature: UInt
val readHit: Bool
val readData: Prediction
```

Write:

```scala
val writeValid: Bool
val writePc: PrunedAddr
val writeHistorySignature: UInt
val writeData: Prediction
```

Required behavior:

- CAM compare key: `{pcTag, historySignature}`
- same-cycle write-read bypass 지원
- redirect 시 flush 불필요 (historySignature staleness guard로 충분, S3 override가 correctness 보장)
- replacement: PLRU 또는 round-robin

Timing risk:

- S1 prediction path에 CAM compare와 mux가 추가된다.
- 3-ahead에서 bypass는 필수이며 S0 lookup / S1 use 구조는 타이밍상 불가능하다 (prefetch 완료가 S0보다 늦음).
- timing closure 실패 시 허용 가능한 fallback은 scheme 자체를 4-ahead로 전환하는 것이다 (완료 N+4, S0 lookup N+4, S1 use N+5, bypass 부담을 S0 레벨로 이동).

---

### 5.5 `bpu/mbtb/MainBtb.scala` — Opportunistic Prefetch Read

2nd read port는 추가하지 않는다.

변경 방향:

- prefetch read request를 받을 수 있는 sideband IO 추가
- regular prediction read가 항상 우선
- prefetch read는 bank conflict가 없을 때만 issue
- conflict가 있으면 drop
- retry queue는 두지 않는다

필요 로직:

```scala
val prefetchReqValid: Bool
val prefetchReqPc: PrunedAddr
val prefetchAccepted: Bool
val prefetchDroppedByBankConflict: Bool
```

mBTB conflict 감소 방향:

- `NumInternalBanks` 증가 검토
- banking hash 개선 검토
- align bank/internal bank conflict counter 추가

---

### 5.6 `bpu/history/phr/Phr.scala` — Prefetch Folded History 생성

현재 PHR은 stage별 folded history를 제공한다.
prefetch path에는 `PC_{X+3}` 기준 TAGE lookup을 위한 speculative folded history가 필요하다.

변경 방향:

- uBTB에서 읽은 `foldedHistoryDelta`를 현재 `s0_foldedPhr`에 적용
- table별 folded history를 보정한 `prefetchFoldedPhr` 생성
- `historySignature` 계산용 hash도 함께 생성

예시 IO:

```scala
val prefetchDeltaValid: Input(Bool())
val prefetchFoldedHistoryDelta: Input(UInt(FoldedHistoryDeltaWidth.W))
val prefetchFoldedPhr: Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
val prefetchHistorySignature: Output(UInt(HistorySignatureWidth.W))  // write key용: hash(prefetchFoldedPhr)
val s1HistorySignature: Output(UInt(HistorySignatureWidth.W))        // read key용: hash(s1_foldedPhr)
```

Apply 방법:

```scala
// AllFoldedHistoryInfo에서 테이블별 slice 범위를 정적으로 결정
for (i <- 0 until numTageTables) {
  val lo = foldedHistorySliceLo(i)
  val hi = foldedHistorySliceHi(i)
  prefetchFoldedPhr(i) := s0_foldedPhr(i) ^ prefetchFoldedHistoryDelta(hi, lo)
}
```

주의:

- 단순 `shift_and_append(takenBits)` 방식이 아니다.
- PHR update가 `pathHash(cfiPc, target)` 영향을 받으므로, commit-time에 table별 folded delta를 미리 계산해 저장하는 방식을 우선 고려한다.
- delta XOR 계산은 `AllFoldedHistoryInfo`의 테이블 순서와 width를 그대로 따른다.

---

### 5.7 `bpu/tage/Tage.scala` / `TageTable.scala` — Opportunistic Prefetch Read

2nd read port는 추가하지 않는다.

변경 방향:

- prefetch read request sideband 추가
- regular read 우선
- prefetch는 table/bank conflict가 없는 경우에만 issue
- conflict 시 drop
- TAGE table banking 확장 또는 hash 개선으로 conflict rate 감소

필요 로직:

```scala
val prefetchReqValid: Bool
val prefetchReqPc: PrunedAddr
val prefetchFoldedPhr: PhrAllFoldedHistories
val prefetchAccepted: Bool
val prefetchDroppedByBankConflict: Bool
```

prefetch result 조립:

- mBTB prefetch result의 branch candidates를 TAGE prefetch result로 direction 보정
- SC correction은 적용하지 않음
- TAGE provider/alt 선택 로직은 prefetch path에도 동일하게 적용

---

### 5.8 `bpu/Bpu.scala` — Main Orchestration

#### (a) uBTB aheadInfo 연결

```scala
val s1_aheadValid = ubtb.io.aheadInfo.valid
val s1_aheadPc = ubtb.io.aheadInfo.bits.aheadPc
val s1_foldedHistoryDelta = ubtb.io.aheadInfo.bits.foldedHistoryDelta
// uBTB에 저장된 historySignature는 prefetch 발행 유효성 필터 용도로만 사용 (optional).
// buffer hit key의 historySignature는 별도 runtime 계산값 사용 (아래 (d)(e) 참고).
```

#### (b) Prefetch request issue

```scala
val s1_prefetchReqValid =
  s1_fire &&
  s1_aheadValid

phr.io.prefetchDeltaValid := s1_prefetchReqValid
phr.io.prefetchFoldedHistoryDelta := s1_foldedHistoryDelta

mbtb.io.prefetchReq.valid := s1_prefetchReqValid
mbtb.io.prefetchReq.pc := s1_aheadPc

tage.io.prefetchReq.valid := s1_prefetchReqValid
tage.io.prefetchReq.pc := s1_aheadPc
tage.io.prefetchReq.foldedPhr := phr.io.prefetchFoldedPhr
```

실제 issue는 mBTB와 TAGE가 모두 conflict-free로 accept할 때만 유효하게 본다.

#### (c) Prefetch result 조립

- mBTB result에서 earliest valid branch candidate 선택
- conditional branch는 TAGE direction 적용
- direct branch는 taken으로 사용
- indirect/return은 buffer write 제외
- taken=false는 fallThrough-style prediction으로 변환

#### (d) Buffer write

write historySignature = prefetch 발행 시(cycle N+2) `phr.io.prefetchHistorySignature`를 result 완료(cycle N+4)까지 파이프라인한 값.

```scala
// prefetch 발행 시 계산: hash(s0_foldedPhr XOR foldedHistoryDelta)
val prefetch_s1_histSig = phr.io.prefetchHistorySignature  // cycle N+2
val prefetch_s2_histSig = RegNext(prefetch_s1_histSig)      // cycle N+3
val prefetch_s3_histSig = RegNext(prefetch_s2_histSig)      // cycle N+4 (write time)

prefetchBuffer.io.writeValid := prefetchResultValid
prefetchBuffer.io.writePc := prefetchPc
prefetchBuffer.io.writeHistorySignature := prefetch_s3_histSig
prefetchBuffer.io.writeData := prefetchPrediction
```

#### (e) Buffer read and S1 prediction replacement

read historySignature = S1 of PC_{X+3}(cycle N+4) 시점의 실제 PHR에서 계산한 값.
speculation이 맞으면 write key == read key → hit.

```scala
// s1_foldedPhr는 현재 S1 시점의 실제 speculative folded history
val s1_histSig = phr.io.s1HistorySignature  // hash(s1_foldedPhr)

prefetchBuffer.io.readPc := s1_startPc
prefetchBuffer.io.readHistorySignature := s1_histSig

when(prefetchBuffer.io.readHit) {
  s1_prediction := prefetchBuffer.io.readData
}.otherwise {
  s1_prediction := originalS1Prediction
}
```

#### (f) S3 override policy

`s3_override` logic은 기존처럼 유지한다.

```scala
s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)
```

buffer hit만으로 override를 suppress하지 않는다.

#### (g) Flush / invalidate

redirect 시 buffer flush는 **불필요**하다.

hit key `{pcTag, historySignature}`가 staleness guard 역할을 한다. redirect 후 실제 history가 달라지면 expectedHistorySignature도 달라져 buffer miss → 기존 uBTB path로 fallback된다. S3 override가 항상 correctness를 보장하므로 wrong-path entry가 buffer에 남아 있어도 문제없다.

단, 다음 경우에는 명시적 flush를 검토한다:

- CSR로 predictor 전체 disable 시
- large predictor (mBTB/TAGE) full reset 시

---

### 5.9 `frontend/Bundles.scala` — FtqToBpuIO 확장

commit-based threeAhead train channel 추가:

```scala
val aheadTrain: Valid[UbtbAheadTrain] = Valid(new UbtbAheadTrain)
```

Bundle 예시:

```scala
class UbtbAheadTrain extends BpuBundle {
  val startPc: PrunedAddr = PrunedAddr(VAddrBits)
  val aheadPc: PrunedAddr = PrunedAddr(VAddrBits)
  val foldedHistoryDelta: UInt = UInt(FoldedHistoryDeltaWidth.W)
  val historySignature: UInt = UInt(HistorySignatureWidth.W)
}
```

---

### 5.10 `ftq/Ftq.scala` — Commit Window 기반 threeAhead Training

현재 `io.toBpu.train`은 resolve queue 기반이다.
threeAhead metadata는 commit된 path 기준으로 학습해야 하므로 별도 commit-window 로직이 필요하다.

변경 방향:

- FTQ commit side에서 `commitPtr ~ commitPtr+3` window 추적
- window 내 4개 fetch block이 모두 committed 되었을 때 train 생성
- `startPc = entryQueue(commitPtr).startPc`
- `aheadPc = entryQueue(commitPtr + 3).startPc`
- window 내 branch/path 정보를 이용해 foldedHistoryDelta 계산
- historySignature 생성
- `io.toBpu.aheadTrain`으로 전달

주의:

- commit path에 branch outcome/path hash 정보가 충분한지 확인 필요
- 부족하면 FTQ entry 또는 별도 side buffer에 commit-time delta 계산용 정보를 추가해야 한다.
- 이 로직은 기존 resolve-based BPU train과 독립적이다.

---

### 5.11 Correctness Guard and Validation Counters

S3 final path를 유지하므로 correctness는 기존 override path가 보장한다.
다만 성능 평가와 안정성 검증을 위해 counter가 필요하다.

필수 counter:

- `prefetchReq`
- `prefetchAccepted`
- `prefetchDroppedByMbtbBankConflict`
- `prefetchDroppedByTageBankConflict`
- `prefetchBufferWrite`
- `prefetchBufferHit`
- `prefetchBufferMiss`
- `prefetchBufferHitSameCycleBypass`
- `prefetchFilteredIndirect`
- `prefetchFilteredReturn`
- `prefetchHitAndS3Match`
- `prefetchHitAndS3Override`
- `prefetchHitButScChanged`
- `prefetchHitButIttageChanged`
- `prefetchHitButRasChanged`

---

### 5.12 변경 범위 요약

| 파일 | 변경 종류 | 난이도 |
|------|----------|--------|
| `bpu/ubtb/Parameters.scala` | threeAhead/history 파라미터 추가 | 낮음 |
| `bpu/ubtb/Bundles.scala` | uBTB entry metadata 추가 | 중간 |
| `bpu/ubtb/MicroBtb.scala` | threeAhead output/train 경로 | 중간 |
| `bpu/mbtb/MbtbTagePrefetchBuffer.scala` | 신규 32-entry CAM buffer | 중간 |
| `bpu/mbtb/MainBtb.scala` | opportunistic prefetch read arbitration | 높음 |
| `bpu/history/phr/Phr.scala` | foldedHistoryDelta 적용 | 높음 |
| `bpu/tage/Tage.scala` / `TageTable.scala` | opportunistic prefetch read arbitration | 높음 |
| `bpu/Bpu.scala` | prefetch orchestration 및 S1 mux | 높음 |
| `frontend/Bundles.scala` | aheadTrain IO 추가 | 낮음 |
| `ftq/Ftq.scala` | commit-window threeAhead training | 높음 |

---

## 6. Open Questions

1. FTQ commit side에 delta 계산에 필요한 branch/path hash 정보가 충분한가?
2. 32-entry CAM이 S1 timing에 들어가도 timing closure가 가능한가? (bypass 포함)
3. mBTB/TAGE bank conflict rate가 scheme 실효 coverage에 얼마나 영향을 주는가? 구현 전 simulation으로 workload별 conflict rate를 측정하고, prefetchAccepted rate가 목표치(예: 70% 이상)에 미달하면 NumInternalBanks 확장 우선 적용.
4. same-cycle write-read bypass의 timing이 S1 mux와 함께 닫히는가?
5. indirect/return 제외 정책이 실제 override 감소 효과를 얼마나 제한하는가?

