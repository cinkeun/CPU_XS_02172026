# frontend_Predictor_analysis.md

- Block: Frontend
- Module: Predictor (BPU)
- Source: BPU.scala, Composer.scala, FauFTB.scala, FTB.scala, Tage.scala, SC.scala, ITTAGE.scala, RAS.scala, Bim.scala
- Protocols: Decoupled (bpu_to_ftq), Valid (redirect/update)
- Key Params: numDup=4, HistoryLength, numBr, FtqSize, MaxMetaLength
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_in_out_seq_diagram.md](./frontend_in_out_seq_diagram.md#group-b)

---

## 1. Module Summary

- **역할**: 3-stage 분기 예측 파이프라인. S1(FauFTB), S2(Main FTB+TAGE base), S3(TAGE+SC+ITTAGE+RAS)로 구성되며, 각 stage에서 FTQ로 예측을 전달하고 후속 stage가 더 정밀한 예측을 하면 FTQ를 override(redirect)한다.
- **위치**: `Frontend.scala` → `Module(new Predictor)` (FrontendInlinedImp 내부)
- **Pipeline stage 수**: 3 register stages (S1/S2/S3), S0은 combinational launch phase

---

## 2. Key Parameters

| Parameter     | Source                             | Default | 영향                                  |
| ------------- | ---------------------------------- | ------- | ------------------------------------- |
| numDup        | `HasBPUConst.numDup`               | 4       | PC/history 복제본 수 (타이밍 클로저용) |
| HistoryLength | `p(XSCoreParamsKey).HistoryLength` | 256     | 전역 분기 이력 길이 (GHR 크기)         |
| numBr         | `p(XSCoreParamsKey).numBr`         | 2       | FTB 엔트리당 분기 슬롯 수              |
| numBrSlot     | `numBr - 1`                        | 1       | 전용 분기 슬롯 수                      |
| totalSlot     | `numBr`                            | 2       | 총 슬롯 (brSlots + tailSlot)           |
| MaxMetaLength | `HasBPUConst.MaxMetaLength`        | 512     | 예측 메타데이터 저장 비트 수           |
| FtqSize       | `p(XSCoreParamsKey).FtqSize`       | 64      | FTQ 크기 (BPU stall 기준)             |
| numBpStages   | 3                                  | 3       | 파이프라인 stage 수                   |

---

## 3. Interfaces

| Port              | Dir | Bitwidth | Protocol  | Description                            |
| ----------------- | --- | -------- | --------- | -------------------------------------- |
| `io.bpu_to_ftq.resp` | out | BpuToFtqBundle | Decoupled | 예측 결과 → FTQ (s1/s2/s3 포함) |
| `io.ftq_to_bpu.redirect` | in | BranchPredictionRedirect | Valid | FTQ→BPU redirect (misprediction) |
| `io.ftq_to_bpu.update` | in | BranchPredictionUpdate | Valid | FTQ→BPU 학습 데이터 |
| `io.ftq_to_bpu.enq_ptr` | in | FtqPtr | — | FTQ 현재 enqueue 포인터 (stall 판단) |
| `io.ctrl` | in | BPUCtrl | — | 각 예측기 enable/disable |
| `io.reset_vector` | in | PAddrBits | — | 리셋 PC |

**BPUCtrl 필드**: `ubtb_enable`, `btb_enable`, `bim_enable`, `tage_enable`, `sc_enable`, `ras_enable`, `loop_enable`

---

## 4. Internal Pipeline / State

### 4.1 Stage 구성

> **핵심**: 모든 predictor는 `s0_startPc`를 **동시에** 입력받는다. (`p.io.startPc := s0_startPc` — `Bpu.scala:192`)
> 출력이 S1/S2/S3에서 나오는 것은 각 predictor의 **lookup latency 차이** 때문이며, 각 stage는 서로 다른 PC를 처리하는 것이 아니다.

```
S0 (combinational)
  ├─ s0_startPc: MuxCase
  │    priority: redirect.target > s3_override→s3_prediction.target > s1_valid→s1_prediction.target > s0_startPcReg
  ├─ s0_startPcReg: RegEnable(s0_startPc, !s0_stall)  — stall 시 PC 유지
  ├─ s0_stall: !(s1_valid || s3_override || redirect.valid)
  ├─ s0_fire: s1_ready && all_predictors_reset_done
  └─ → 모든 predictor에 s0_startPc 전달 (동시, 병렬)

S1 (REG: s1_valid, s1_startPc = RegEnable(s0_startPc, s0_fire))
  ├─ [1-cycle lookup 결과] ubtb → 1개 후보 (항상 taken, index 0)
  ├─ [1-cycle lookup 결과] abtb → 최대 8개 후보 (index 1..8)
  │    └─ 두 출력을 concat: s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction  (총 9개 풀)
  │       ※ 두 예측기가 경쟁하는 것이 아님 — 9개 후보를 하나의 Vec으로 합쳐서 position으로 정렬
  ├─ [1-cycle lookup 결과] utage → conditional 분기 방향 보정 (s1_utageHitMask: 동일 cfiPosition 후보에 override)
  ├─ [1-cycle lookup 결과] uras → ret target 제공 (s1_isRet && uras.specOut.isCanUse → s1_prediction.target)
  ├─ [always on]          fallThrough → sequential fallthrough prediction (fallback)
  ├─ s1_takenMask: 9개 각각에 대해 taken 여부 계산 (conditional → utage or BTB 방향, direct/indirect → 항상 taken)
  ├─ s1_firstTakenBranchOH: CompareMatrix로 takenMask=1인 후보 중 최소 cfiPosition 1개 선택
  │    └─ s1_firstTakenBranchOH(0)=1 → uBTB 승리 / (0)=0 → ABTB 후보 중 1개 승리
  ├─ s1_prediction: 선택된 단일 후보 (or fallThrough) → .target이 다음 cycle S0 PC
  ├─ s1_fire = s1_valid && s2_ready && prediction.ready
  └─ if s1_fire → prediction.valid=1, s3Override=0 → FTQ (s1_startPc, s1_prediction 전달)

S2 (REG: s2_valid, s2_startPc = RegEnable(s1_startPc, s1_fire))  — same PC as S1
  ├─ [2-cycle lookup 결과] mbtb → s2_mbtbResult (main BTB, 더 정확한 분기 정보)
  ├─ [2-cycle lookup 결과] tage → tage.io.prediction (TAGE 테이블 방향 예측)
  ├─ [2-cycle lookup 결과] sc → sc.io.scTakenMask (Statistical Corrector 보정)
  ├─ s2_takenMask: mbtb entries에 tage/sc 방향 결과 적용
  ├─ s2_fire = s2_valid && s3_ready
  ├─ s2_flush = s3_flush || s3_override  ← BPU 내부 flush (FTQ에 전달 안 됨)
  └─ [FTQ 미전달] S2 결과는 S3로 래치될 뿐, FTQ로 직접 전송 없음

S3 (REG: s3_valid, s3_startPc = RegEnable(s2_startPc, s2_fire))  — same PC as S1/S2
  ├─ s3_mbtbResult = RegEnable(s2_mbtbResult, s2_fire)  — S2 결과 래치
  ├─ s3_takenMask  = RegEnable(s2_takenMask, s2_fire)
  ├─ [3-cycle lookup 결과] ittage → ittage.io.prediction.target (간접 분기 target)
  ├─ [3-cycle lookup 결과] ras   → ras.io.topRetAddr (full RAS ret 주소)
  ├─ s3_prediction.target: MuxCase(fallThrough, [(taken&&useRas)→ras, (taken&&useIttage)→ittage, taken→mbtb])
  ├─ s3_s1Prediction = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire)  — S1 예측 전파
  ├─ s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)
  ├─ s3_fire = s3_valid  (항상 fire — 결과 항상 소비)
  └─ if s3_override → prediction.valid=1, s3Override=1 → FTQ (s3_startPc, s3_prediction 전달)
```

### 4.2 History Register 관리

- `phr` (Phr module): Path History Register — `s0_foldedPhr`, `s1_foldedPhr` 등 folded 버전 제공
- `commonHR` (CommonHR module): Global History Register — `s0_commonHR` 제공
- S3 fire 시 commonHR update: `startPc`, `target`, `taken`, `firstTakenBranch`, branch position

### 4.3 FTQ 전달 구조 (BpuToFtqIO)

```
prediction: DecoupledIO[BpuPrediction]
  ├─ startPc: PrunedAddr       — s1_startPc 또는 s3_startPc (s3Override 시)
  ├─ target:  PrunedAddr       — 예측된 다음 PC
  ├─ taken:   Bool
  ├─ cfiPosition: UInt
  ├─ attribute: CfiAttribute   — isDirect/isIndirect/isConditional/isReturn
  ├─ s3Override: Bool          — true이면 FTQ가 기존 entry 갱신(override), false이면 신규 enq
  └─ (via fromStage())

s3FtqPtr: FtqPtr               — override 대상 entry 인덱스 (RegEnable 2단)
meta: Valid[BpuMeta]           — s3_valid 시: redirectMeta / resolveMeta / commitMeta
```

---

## 5. Functionality

### 5.1 PC 생성 (S0)

- `s0_startPc` MuxCase 우선순위 (높은 순):
  1. `redirect.valid` → `redirect.bits.target` (backend mispred 복구)
  2. `s3_override` → `s3_prediction.target` (S3 override)
  3. `s1_valid` → `s1_prediction.target` (S1 예측 target으로 다음 블록 fetch)
  4. else → `s0_startPcReg` (stall: 이전 PC 유지)
- `s0_stall = !(s1_valid || s3_override || redirect.valid)` → 유효한 PC 소스가 없을 때

### 5.2 S1 예측 조립 (1-cycle latency predictors)

**후보 풀링 (Bpu.scala:266)**:
```
s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction
//                  ↑ uBTB 최대 1개 (index 0)     ↑ ABTB 최대 8개 (index 1..8)
//                  → 총 최대 9개 후보를 하나의 Vec으로 concat

// uBTB: tag = fetch block start PC[22:1] → fully-assoc 비교 → 최대 1 hit
//        (32 entries × 1 entry/fetch block → 같은 fetch block은 최대 1 entry → max 1 hit)
// ABTB: tag = fetch block start PC[24:1] → 같은 fetch block 내 여러 분기가 각 way에 저장
//        → 여러 way가 동시에 hit 가능 → 최대 8개 Valid[Prediction] 출력
// 두 예측기의 출력을 하나의 후보 풀로 합쳐서 position 비교로 단일 예측 결정.
```

**takenMask 계산 (Bpu.scala:270-278)**:
- `s1_utageHitMask[i]`: utage의 cfiPosition이 후보 i와 일치하면 → utage 방향 사용
- `s1_takenMask[i] = pred[i].valid && (isDirect || isIndirect || (isConditional && Mux(utageHit, utageTaken, pred.taken)))`

**단일 예측 선택 (Bpu.scala:299-304)**:
```
s1_compareMatrix      = CompareMatrix(s1_btbPrediction[*].cfiPosition)  // 9개 position 페어와이즈 비교
s1_firstTakenBranchOH = compareMatrix.getLeastElementOH(s1_takenMask)   // taken인 것 중 최소 position → 1-hot
s1_firstTakenBranch   = Mux1H(s1_firstTakenBranchOH, s1_btbPrediction)  // 선택된 1개 후보

// 승자 판별 (debug signal, Bpu.scala:311-314):
//   s1_firstTakenBranchOH(0) = 1  → uBTB 후보 승리
//   s1_firstTakenBranchOH(0) = 0  → ABTB 후보 중 1개 승리 (더 작은 position)
//   동일 position이면 index 0 (uBTB) 우선 (OHToUInt 특성)
```

- `s1_prediction = Mux(s1_taken, s1_firstTakenBranch.bits, fallThrough.io.prediction)`
- ret 분기 시: `s1_prediction.target := uras.io.specOut.retTarget` (fast RAS)
- `s1_prediction.target` → 다음 cycle `s0_startPc` (Bpu.scala:439: `s1_valid → s1_prediction.target`)

### 5.3 S2 예측 조립 (2-cycle latency predictors)

- `s2_mbtbResult = mbtb.io.result` — main BTB 결과 (S0 PC로 lookup, 2사이클 후 결과)
- `s2_condTakenMask`: mbtb conditional 분기에 tage/sc 방향 적용
  - `sc.io.scUsed` → SC가 사용되면 `sc.io.scTakenMask` 우선
  - `tage.useProvider` → provider table 예측 사용
  - else → mbtb의 기본 방향
- `s2_takenMask = s2_condTakenMask || s2_jumpMask` (direct/indirect는 항상 taken)
- FTQ에 미전달 — S3로만 전파

### 5.4 S3 예측 조립 및 override (3-cycle latency predictors)

- `s3_mbtbResult = RegEnable(s2_mbtbResult, s2_fire)` — S2 mbtb 결과 래치
- `s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit`
- `s3_useRas = s3_firstTakenBranch.bits.attribute.isReturn`
- `s3_prediction.target` 우선순위: ras.topRetAddr > ittage.prediction.target > mbtb.target > fallThrough
- `s3_s1Prediction = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire)` — 동일 PC의 S1 예측 전파
- `s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)` — 두 예측이 다를 때

### 5.5 FTQ 전달 경로

```scala
// Bpu.scala:415-421
io.toFtq.prediction.valid := s1_valid && s2_ready || s3_override
when(s3_override) {
  io.toFtq.prediction.bits.fromStage(s3_startPc, s3_prediction)  // override 경로
}.otherwise {
  io.toFtq.prediction.bits.fromStage(s1_startPc, s1_prediction)  // 정상 경로
}
io.toFtq.prediction.bits.s3Override := s3_override
```

- **정상 경로**: s1_fire → s1_prediction을 FTQ에 신규 enq (s3Override=0)
- **override 경로**: s3_override → s3_prediction으로 기존 entry(s3FtqPtr) 갱신 (s3Override=1), prediction.ready 비의존

---

## 6. Flow / Backpressure Control

| 조건 | 동작 | 코드 근거 |
| ---- | ---- | --------- |
| FTQ full (`prediction.ready=0`) | `s1_fire=0` → S1 stall, S0도 stall 연쇄 | `s1_fire := s1_valid && s2_ready && prediction.ready` |
| s3_override 발생 | `s2_flush=1`, `s1_flush=1` → BPU 내부 S1+S2 in-flight kill (다음 PC들) | `s2_flush := s3_flush || s3_override` |
| redirect.valid | `s3_flush=1` → s3_valid 소거, s2_flush, s1_flush 연쇄 | `s3_flush := redirect.valid` |
| s0_stall | `s0_startPcReg` 유지 (PC 동결), predictors 입력 고정 | `s0_stall := !(s1_valid \|\| s3_override \|\| redirect.valid)` |
| s3_override + FTQ full | s3_override는 `bpuS3Redirect`로 prediction.ready 무관하게 FTQ 갱신 | `Ftq.scala: bpuS3Redirect = prediction.valid && s3Override` |

---

## 7. Error / Exception Handling

- BPU 자체는 예외를 생성하지 않음
- misprediction 감지: FTQ가 ROB commit 결과 비교 후 `toBpu.redirect.valid` 전송
- `BranchPredictionUpdate.mispred_mask[numBr+1]`: 어느 slot이 틀렸는지 표시
- `false_hit`: FTB hit이었으나 실제로는 다른 entry (ghost entry 등)
- redirect 수신 시 speculatively updated GHR 롤백: `ghistPtrGen` 우선순위 복원

---

## 8. Timing Hints

| Critical Path | 설명 |
| ------------- | ---- |
| S0 PC mux | `s0_startPc` MuxCase: redirect/s3_override/s1_valid/hold 4단계 우선순위 — 타이밍 크리티컬 |
| S1 ubtb/abtb lookup | ubtb: fully-associative tag 비교; abtb: ahead fetch + tag 비교 (병렬) |
| S1 s1_prediction 조립 | CompareMatrix(position): 최소 cfiPosition 선택 — wide comparator |
| S2 mbtb SRAM | main BTB SRAM read latency + tag 비교 |
| S2 tage/sc | 다수 TAGE 테이블 동시 lookup (각 다른 history 길이), SC 적용 |
| S3 ittage/ras | indirect target 테이블 lookup (ittage), RAS top 읽기 (ras) |
| phr folded history | folded path history 다단 XOR 트리 — compLen 비트 전체 |
| s3_override 판정 | `s3_prediction === s3_s1Prediction`: 다수 필드 전체 비교 — wide equality |

---

## 9. Pseudocode

> **주의**: 세 predictor 그룹은 모두 동일한 `s0_startPc`를 입력받는다.
> S1/S2/S3에서 결과가 나오는 것은 각 predictor의 **lookup latency 차이** 때문이며,
> S2는 S1의 다음 PC가 아닌 **같은 s0_startPc**에 대한 더 정확한 결과다.

```
=== Cycle T: S0 ===
s0_startPc = MuxCase(s0_startPcReg, [
  redirect.valid  → redirect.bits.target,
  s3_override     → s3_prediction.target,
  s1_valid        → s1_prediction.target
])
s0_stall = !(s1_valid || s3_override || redirect.valid)
if not s0_stall:
  s0_startPcReg := s0_startPc

// 모든 predictor에 s0_startPc 동시 전달 (lookup 시작)
for p in [ubtb, abtb, utage, uras, fallThrough, mbtb, tage, sc, ittage, ras]:
  p.io.startPc := s0_startPc

s0_fire = s1_ready && all_predictors_reset_done


=== Cycle T+1: S1 ===  (s0_startPc에 대한 1-cycle latency 결과 수신)
if s0_fire:
  s1_startPc := RegEnable(s0_startPc)  // = 위 S0의 s0_startPc
  s1_valid   := true

// 1-cycle lookup 결과 (s0_startPc 기준):
// uBTB(1개) + ABTB(최대 8개) → 총 9개 후보 풀로 concat (경쟁 아님)
s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction  // index 0=uBTB, 1..8=ABTB
s1_utageHitMask  = [utage.prediction.cfiPosition == pred.cfiPosition for pred in btbPred]
s1_takenMask = [pred.valid && (direct || indirect || (conditional && Mux(utageHit, utageTaken, pred.taken)))]
// CompareMatrix: 9개 후보 중 takenMask=1인 것 중 최소 cfiPosition 선택 → 단일 s1_prediction
s1_firstTakenBranchOH = CompareMatrix(positions).getLeastElementOH(takenMask)
s1_prediction = Mux(s1_taken, firstTakenBranch.bits, fallThrough.io.prediction)
// → s1_prediction.target이 다음 cycle s0_startPc 결정 (s1_valid 경로)
if s1_prediction.attribute.isReturn && uras.specOut.isCanUse:
  s1_prediction.target := uras.io.specOut.retTarget

s1_fire = s1_valid && s2_ready && prediction.ready
if s1_fire:
  // FTQ로 전달 (신규 enq, s3Override=0)
  prediction.valid = 1
  prediction.bits  = fromStage(s1_startPc, s1_prediction)
  prediction.bits.s3Override = 0
  // s0_startPc 다음 사이클: s1_prediction.target를 다음 블록 PC로 사용

if s1_flush (= s2_flush):
  s1_valid := false


=== Cycle T+2: S2 ===  (s0_startPc에 대한 2-cycle latency 결과 수신)
if s1_fire:
  s2_startPc := RegEnable(s1_startPc)  // = 여전히 원래 s0_startPc
  s2_valid   := true

// 2-cycle lookup 결과 (s0_startPc 기준):
s2_mbtbResult   = mbtb.io.result           // main BTB (ubtb/abtb보다 정확)
s2_condTakenMask = mbtb entries에 tage/sc 방향 적용:
  Mux(sc.scUsed → sc.scTaken, tage.useProvider → tage.providerPred, else → mbtb.taken)
s2_takenMask = s2_condTakenMask || jumpMask

// FTQ에 직접 전달 없음 — S3로만 전파
s2_fire = s2_valid && s3_ready
if s2_flush (= s3_flush || s3_override):
  s2_valid := false


=== Cycle T+3: S3 ===  (s0_startPc에 대한 3-cycle latency 결과 수신)
if s2_fire:
  s3_startPc := RegEnable(s2_startPc)  // = 여전히 원래 s0_startPc
  s3_valid   := true

// S2 결과 래치 (S3로 파이프라인 전달):
s3_mbtbResult        = RegEnable(s2_mbtbResult, s2_fire)
s3_takenMask         = RegEnable(s2_takenMask, s2_fire)
s3_firstTakenBranch  = Mux1H(firstTakenBranchOH, s3_mbtbResult)
s3_s1Prediction      = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire)  // 동일 PC의 S1 예측

// 3-cycle lookup 결과 (s0_startPc 기준):
s3_useRas    = s3_firstTakenBranch.attribute.isReturn
s3_useIttage = s3_firstTakenBranch.attribute.needIttage && ittage.prediction.hit
s3_prediction.target = MuxCase(fallThrough.target, [
  (taken && useRas)    → ras.io.topRetAddr,
  (taken && useIttage) → ittage.io.prediction.target,
  taken                → s3_firstTakenBranch.bits.target
])

// s1 예측과 비교 (동일 s0_startPc에 대해)
s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)
if s3_override:
  // FTQ로 전달 (override, s3Override=1) — prediction.ready 무관
  prediction.valid = 1
  prediction.bits  = fromStage(s3_startPc, s3_prediction)
  prediction.bits.s3Override = 1
  io.toFtq.s3FtqPtr = s3_ftqPtr  // override 대상 entry 인덱스
  // BPU 내부: 다음 PC들에 대한 in-flight 소거
  s2_flush := 1  // s1_flush도 = s2_flush
  s0_startPc 다음 사이클: s3_prediction.target (2순위 MUX)

// s3_fire = s3_valid (항상)
if s3_valid:
  commonHR.update(s3_startPc, s3_prediction)
  ras.specIn := {s3_startPc, s3_prediction}

s3_fire = s3_valid  // 항상 fire


=== on redirect (backend mispred) ===
s3_flush := redirect.valid → s3/s2/s1_valid 소거
phr rollback using redirect.bits meta
ras rollback using redirect.bits meta
s0_startPc 최우선: redirect.bits.target
```

---

### 9.1 Conditional Branch 예측 (mBTB + TAGE + SC)

```
=== [Case 1: Conditional Branch] mBTB + TAGE + SC ===
// 핵심: direction(taken/not-taken)만 결정. target은 mBTB 고정.

// --- S0: SRAM read requests (동시 발송) ---
mbtb.readReq(alignBankIdx=PC[5], internalBankIdx=PC[7:6], setIdx=PC[15:8])
for t in tage.tables[0..7]:
  t.readReq(setIdx = fold(PC XOR pathHist XOR globalHist, t.histLen),
            tag    = fold(PC XOR globalHist, TagWidth))
for t in sc.tables:  // path-based + global-based
  t.readReq(setIdx = fold(pathHist XOR PC, t.histLen))

// --- S1: SRAM responses arrive ---
mbtb_s1_rawEntries = mbtb.readResp()     // tag compare는 S2에서
tage_s1_rawResps   = tage.tables.readResp()
sc_s1_rawResps     = sc.tables.readResp()

// --- S2: tag compare + direction 결정 ---

// [mBTB] tag compare
for each way i:
  mbtb_hit[i]   = entry[i].valid && entry[i].tag == PC[31:16]
  mbtb_taken[i] = mbtb.counterSram[i].isPositive  // 2-bit saturating counter

// [TAGE] provider 선택 (가장 긴 history를 가진 hit table)
for each way i:  // = mBTB result slot 기준
  hitTableMask    = [tage.tables[j].tag == computedTag[j] for j in 0..7]
  providerTableOH = getLongestHistTableOH(hitTableMask)
  provider        = tage.tables[providerTableOH]
  hasAlt          = any hit besides provider
  alt             = tage.tables[altTableOH]        // second longest hit

  useProvider  = hasProvider && !(useAltOnNa && provider.takenCtr.isWeak)
  providerPred = provider.takenCtr.isPositive      // MSB of 2-bit ctr
  altPred      = alt.takenCtr.isPositive

// [SC] adaptive-threshold confidence check
for each way i:
  percsum[j] = sc.tables[j].ctr * 2 + 1           // getPercsum (signed partial sum)
  scSum      = sum(percsum for all SC tables)

  // TAGE provider confidence에 따라 threshold 조정
  tageConfHigh = provider.takenCtr.isSaturate      // 강한 확신 (both ends)
  tageConfMid  = provider.takenCtr.isMid           // 중간
  // tageConfLow  = otherwise

  adaptiveThres = scThreshold >> (1 if confHigh else 2 if confMid else 3)

  scUsed[i]  = mbtb_hit[i] && hasProvider && aboveThreshold(|scSum|, adaptiveThres)
  scTaken[i] = scSum >= 0                          // 부호로 방향 결정

// [최종 direction] 우선순위: SC > TAGE provider > TAGE alt > mBTB counter
for each way i:
  condTaken[i] = mbtb_hit[i] && isConditional[i] &&
    MuxCase(mbtb_taken[i],         // 기본값: mBTB 2-bit counter
      scUsed[i]   → scTaken[i],   // SC 활성: scSum 부호 사용
      useProvider → providerPred, // TAGE provider hit
      hasAlt      → altPred       // TAGE alt hit
    )

// --- S3: first taken 선택 + target + override ---
takenMask[i]     = condTaken[i] || jumpTaken[i]   // jumpTaken = isDirect||isIndirect
firstTakenOH     = CompareMatrix(positions).getLeastElementOH(takenMask)
firstTakenBranch = Mux1H(firstTakenOH, s3_mbtbResult)

s3_prediction.taken       = takenMask.reduce(||)
s3_prediction.cfiPosition = firstTakenBranch.bits.cfiPosition
s3_prediction.target      = reconstruct(PC, firstTakenBranch.bits.targetLowerBits,
                                             firstTakenBranch.bits.targetCarry)
// conditional branch target은 mBTB에서만 — TAGE/SC는 direction만 보정

s3_override = s3_valid && (s3_prediction != s3_s1Prediction)
```

---

### 9.2 Indirect Jump 예측 (mBTB + ITTage, non-return)

```
=== [Case 2: Indirect Jump] mBTB + ITTage (isReturn=false) ===
// 핵심: direction은 항상 taken. target 정확도만이 문제.

// --- S0: SRAM read requests ---
mbtb.readReq(alignBankIdx=PC[5], internalBankIdx=PC[7:6], setIdx=PC[15:8])
// ITTage는 S0에서 읽지 않음 (power opt: s1_isIndirect 대기)
// ※ 이론상 S0 speculative read → S2에서 prediction 완성 가능하지만 현재 미채택

// --- S1: mBTB SRAM resp + ITTage read req ---
mbtb_s1_rawEntries = mbtb.readResp()
s1_isIndirect      = firstTakenBranch.attribute.needIttage  // abtb/ubtb s1 결과 기반

if s1_isIndirect:
  for t in ittage.tables:
    t.readReq(setIdx = fold(hist XOR PC, t.histLen),
              tag    = fold(hist XOR PC, TagWidth))

// --- S2: mBTB tag compare + ITTage SRAM resp + provider 선택 ---

// [mBTB] tag compare
for each way i:
  mbtb_hit[i]    = entry[i].valid && entry[i].tag == PC[31:16]
  mbtb_target[i] = reconstruct(PC, entry[i].targetLowerBits, entry[i].targetCarry)
  jumpTaken[i]   = mbtb_hit[i] && isIndirect   // 항상 taken (direction 예측 불필요)

// [ITTage] provider 선택 (가장 긴 history를 가진 hit table)
ittage_hitMask    = [ittage.tables[j].tag == computedTag[j] for j in tables]
ittage_providerOH = getLongestHistTableOH(ittage_hitMask)
ittage_provided   = any(ittage_hitMask)
ittage_target     = ittage.tables[ittage_providerOH].target  // 전체 target 저장

// s2_ittageTarget 래치 → S3에서 io.prediction.target으로 출력

// --- S3: target 선택 + override ---
firstTakenBranch = Mux1H(firstTakenOH, s3_mbtbResult)

// 우선순위: RAS(isReturn) > ITTage(needIttage && hit) > mBTB target
s3_useRas    = firstTakenBranch.bits.attribute.isReturn
s3_useIttage = firstTakenBranch.bits.attribute.needIttage && ittage.prediction.hit

s3_prediction.taken  = true   // indirect는 항상 taken
s3_prediction.target = MuxCase(firstTakenBranch.bits.target,  // fallback: mBTB
  (s3_taken && s3_useRas)    → ras.topRetAddr,               // return: RAS
  (s3_taken && s3_useIttage) → ittage.prediction.target      // indirect: ITTage
)
// ※ isReturn 시에도 mBTB entry 존재 (attribute=Return) → target은 RAS로 override

s3_override = s3_valid && (s3_prediction != s3_s1Prediction)
```

---

## 10. Notes / Assumptions

- **동일 PC 입력**: 모든 predictor가 `s0_startPc`를 공유 — `Bpu.scala:192 p.io.startPc := s0_startPc`
- **S1 예측 = fast path**: ubtb+abtb+utage+uras 결과. 1사이클이므로 즉시 FTQ enq 및 다음 블록 fetch 시작
- **S3 override 조건**: `!(s3_prediction === s3_s1Prediction)` — 동일 PC에 대해 S3 결과가 S1과 다를 때만 발생 (target, taken, cfiPosition, attribute 모두 비교)
- **S2는 FTQ에 직접 미전달**: S2 mbtb/tage/sc 결과는 S3 stage로 전파되어 s3_prediction 조립에 사용. 이전 아키텍처(FauFTB→FTB s2_redirect)와 다름
- **phr (Phr), commonHR (CommonHR)**: 이전 아키텍처의 GHR/FoldedHistory를 대체. path history + global history 분리 관리
- **uras (MicroRas)**: S1에서 ret target 빠르게 제공. full RAS(ras)의 specOut은 S3에서 사용
- **fastTrain**: `s3_valid` 시 `BpuFastTrain` 신호 생성 → abtb fast training에 사용 (`Bpu.scala:183-188`)

---

## 11. BTB / FTB 계층 구조 상세

### 11.1 구조 개요

이 버전(KunMingHu v3)은 기존 `uFTB → FTB` 2단 구조에서 **uBTB → ABTB → MBTB** 3단 BTB 계층으로 발전되었다.
각 BTB는 서로 다른 stage에서 결과를 내며, 각기 다른 방향 예측기와 페어를 이룬다.

```
S1 (1-cycle): uBTB + ABTB ──→ uTAGE (방향 보정)
S2 (2-cycle): MBTB ──────────→ TAGE(8-table) + SC (방향 보정)
S3 (3-cycle): (BTB 없음) ───→ ITTAGE (indirect target) + RAS (return target)
```

**BTB 방식 분류**:

일반적인 BTB 설계는 두 가지로 분류된다:

| 방식 | tag 기준 | 동작 |
|---|---|---|
| **Block BTB** (fetch-block indexed) | **fetch block start PC** | entry 1개가 fetch block 1개 커버 → lookup 1회로 해당 fetch block의 첫 taken 분기 예측 반환 |
| **Individual BTB** (branch-indexed) | **개별 branch instruction PC** | entry 1개가 branch 1개 커버 → 한 fetch block에 N개 분기 있으면 N번 lookup 필요 |

이 구현의 **uBTB / ABTB / MBTB는 모두 Block BTB**다:

| BTB | tag 단위 | lookup당 반환 수 |
|---|---|---|
| **uBTB** | 64B fetch block (PC[22:1]) | **최대 1개** (32 entries fully-assoc, 동일 fetch block은 최대 1 entry) |
| **ABTB** | 64B fetch block (PC[24:1]) | **최대 8개** (8 ways가 동일 tag 공유, 한 fetch block의 여러 분기 저장) |
| **MBTB** | 32B half-block (tag=PC[31:16]) | **최대 8개** (2 AlignBanks × 4 ways, 각 half-block에 다중 분기 저장) |

---

### 11.2 uBTB (Micro BTB) — S1 Stage

| 항목 | 값 |
|---|---|
| BTB 방식 | **Block BTB** — tag = fetch block start PC[22:1], 1 entry per fetch block, 1 taken branch per entry |
| 구조 | **Fully Associative Cache** |
| Entry 수 | **32** |
| Tag Width | **22 bits** |
| Target Width | 22 bits (2B aligned) |
| Useful Counter | 2 bits |
| Replacer | **PLRU** (least-useful first) |
| History 사용 | **없음** |
| 예측 Latency | **1 cycle** |

**AddrField 비트 구조** (`ubtb/Helpers.scala:25`, `ubtb/Parameters.scala`):
```
PC bit : [ 0  ] [    22:1    ] [ VAddrBits-1:23 ]
field  :  inst       tag           unused
          Off    (22 bits)
```

| field | PC bits | 비고 |
|---|---|---|
| instOffset | PC[0:0] | 1 bit, always 0 (RVC 2B 정렬) |
| tag | **PC[22:1]** | 22 bits, 분기 예측에 사용 |
| targetLower (extraField) | **PC[22:1]** | 22 bits (tag와 동일 위치, 목적지 주소 저장용) |

**Lookup 방식** (`MicroBtb.scala:72`):
```
// 32 entries 전체 동시 병렬 비교 (fully associative)
s1_tag   = PC[22:1]
s1_hitOH = entries.map(e => e.valid && e.tag === s1_tag)

// Hit 시: 항상 taken 예측 (always-taken predictor)
prediction.taken       := s1_hit
prediction.cfiPosition := hitEntry.slot1.position
prediction.target      := reconstruct(startPc, hitEntry.slot1.target)
```

> **특징**: set index 없음. 전체 tag 직접 비교. PC 상위 비트(PC[VAddrBits-1:23])는 무시되므로, 주소 공간이 넓은 경우 aliasing 가능.

**Entry Contents** (`ubtb/Bundles.scala:32`):
```
MicroBtbEntry {
  tag:       UInt(22)           // PC[22:1], tag 비교용
  usefulCnt: SatCtr(2-bit, signed) // 유용성: SaturateNegative = invalid

  slot1 {                       // 주 예측 분기 (taken candidate)
    position:       UInt(5)     // fetch block 내 분기 위치 (32B-align 기준 instruction slot)
    attribute:      BranchAttribute  // branchType(2) + rasAction(2) = 4 bits
    target:         UInt(22)    // partial target: branch target PC의 [22:1]
    isStaticTarget: Bool        // 동일 target만 관측됐는지 (aliasing 검출용)
  }
  slot2 {                       // TODO: 2-taken — fetch block 내 두 번째 taken 분기 후보 (현재 항상 valid=false, 미사용)
    position:  UInt(5)
    attribute: BranchAttribute
    target:    UInt(22)
    valid:     Bool             // slot2 유효 여부 (현재 항상 false)
    taken:     Bool             // slot2 예측 방향 (현재 미참조)
  }
}
```

**공통 타입 정의** (`bpu/Bundles.scala`):
```
BranchAttribute (4-bit) {
  branchType: 2-bit { None=0, Conditional=1, Direct=2, Indirect=3 }
  rasAction:  2-bit { None=0, Pop=1(ret), Push=2(call), PopAndPush=3 }
  // needIttage = isIndirect && !hasPop
}

TargetCarry (2-bit) {           // partial target 경계 보정용
  Fit=0       : target upper == startPc upper (carry 없음)
  Overflow=1  : target가 상위 영역으로 넘어감 → upper + 1
  Underflow=2 : target가 하위 영역 → upper - 1
  // uBTB: EnableTargetFix=false → TargetCarry 미저장, startPc upper 그대로 사용
  // MBTB: 항상 저장
}

CfiPosition (5-bit): 32B-align 기준 instruction slot index (0..31 / 2B = 최대 16 insts per 32B)
```

**Next PC 결정** (`MicroBtb.scala:79`):
```
if (s1_hit):
  nextPc.taken       = true           // always-taken
  nextPc.cfiPosition = slot1.position
  nextPc.attribute   = slot1.attribute
  // partial target → full VAddr 재구성
  nextPc.target = Cat(
    startPc[VAddrBits-1 : 23],  // upper: EnableTargetFix=false이므로 startPc 상위 그대로
    slot1.target[21:0],          // 22-bit partial target (= branch target PC[22:1])
    0.U(1)                       // LSB=0: 2B 정렬
  )
else:
  → fallThrough 예측 사용
```

**2-taken 기능 현황 (TODO)**:

```
설계 의도:
  - 하나의 fetch block에 slot1 + slot2, 두 개의 taken 분기를 동시에 저장/예측
  - slot1 = 주 taken 분기 (always-taken), slot2 = 두 번째 taken 분기 후보 (방향 bit 포함)
  - 현재 uBTB는 1개의 taken 분기(slot1)만 처리 — 2-taken 구현 시 처리량 향상 기대

현재 상태:
  - Bundles.scala:50-55 : slot2 필드 정의됨 (valid, taken, position, attribute, target)
  - MicroBtb.scala:193  : 학습 시 slot2.valid := false.B 고정 (항상 비활성화)
  - MicroBtb.scala:78-83: 예측 로직은 slot2 전혀 미참조 — slot1만 사용
  - 전체 codebase에서 slot2 참조: 선언부 + false.B 초기화 2곳뿐

결론: 데이터 구조만 예약, 예측·학습 로직 미구현 (MicroBtb.scala 클래스 선언부 // TODO: 2-taken)
```

**페어 방향 예측기**: `uTAGE` (S1 동시 결과, `s1_utageHitMask`로 방향 override)

---

### 11.3 ABTB (Ahead BTB) — S1 Stage

#### 11.3.0 왜 ABTB가 존재하는가

**문제 1: uBTB의 용량 한계**

uBTB는 32 entries fully-associative 구조다. 큰 프로그램에서는 cold miss가 많아 coverage가 부족하다.

**문제 2: Fully-Associative는 확장 불가**

entry 수가 늘수록 비교기가 선형으로 증가 → 타이밍·면적 비용 급증. Set-Associative 구조가 필요하다.

**문제 3: Set-Associative SRAM은 그냥 쓰면 S1 타이밍을 못 맞춘다**

Set-Associative BTB를 단순히 도입하면 PC_B는 uBTB 출력 재구성 후 사이클 중간에 확정되어 SRAM 접근이 늦게 시작된다:

```
[ahead 트릭 없이 일반 Set-Assoc BTB를 쓴다면]

Cycle N:   BPU S0 = PC_A.
Cycle N+1: uBTB 출력으로 PC_B 확정 → 사이클 중간
           일반 BTB: setIndex(PC_B) → SRAM 읽기 사이클 중간에 시작
Cycle N+2: BPU S1 = PC_B.
           SRAM 응답이 사이클 중반 도착 → tag 비교 후반 → S1 타이밍 위반!
           → 결과를 S2에서야 사용 가능 (1 cycle 손실)

[ABTB의 ahead 트릭]

Cycle N:   BPU S0 = PC_A.  PC_A는 이미 안정된 값.
           ABTB S0: setIndex(PC_A)로 SRAM 읽기 시작 → 사이클 초반, 풀 사이클 확보
Cycle N+1: SRAM 응답 사이클 초반 도착. s1_entries 안정.
           s1_startPc = io.startPc = PC_B (이미 안정)
           tag 비교 사이클 초반 시작, 결과 사이클 말 래치
Cycle N+2: BPU S1 = PC_B.  s2_* 레지스터에서 조합 출력 → S1 타이밍 충족!
```

ahead 트릭의 핵심: **SRAM 접근을 1 cycle 앞당겨서**, 큰 Set-Associative SRAM도 S1 타이밍 안에 결과를 낼 수 있게 한다. 트릭 없이는 ABTB는 S2 predictor로 밀려난다.

**문제 4: uBTB는 Multi-Branch 미지원**

uBTB는 slot1 1개만 저장. ABTB는 tag = fetch block PC이므로 한 fetch block의 여러 분기를 8개 way에 동시 저장 → 더 풍부한 분기 커버리지.

**요약:**

| 이유 | 설명 |
|---|---|
| 용량 부족 | uBTB 32 entries → coverage 부족 |
| Scalability | Fully-Assoc 확장 불가 → Set-Assoc 필요 |
| Timing | Set-Assoc SRAM은 S1 타이밍 불가 → ahead 트릭으로 해결 |
| Multi-Branch | 8 ways로 한 fetch block의 여러 분기 동시 캐시 |

---

| 항목 | 값 |
|---|---|
| BTB 방식 | **Block BTB (다중 분기)** — tag = fetch block start PC[24:1], 여러 way가 동일 tag 공유, 최대 8개 분기 동시 반환 |
| 구조 | **Set-Associative, Banked** |
| Total Entries | **1,024** |
| Banks | **4** (read-write conflict 해소) |
| Ways | **8** per set |
| Sets per bank | **32** (= 1024 / 8 / 4) |
| Tag Width | **24 bits** |
| Target Lower Bits | 22 bits (2B aligned) |
| Taken Counter | 2-bit |
| Write Buffer | 4 entries |
| Replacer | PLRU |
| History 사용 | **없음** |
| 예측 Latency | **기준에 따라 다름**: BPU s0 입력 PC 기준 2 cycle / BPU s1 current block 기준 1 cycle |

> 기준 정리:
> - BPU s0 입력 PC(PC_A) 기준: `PC_A -> PC_B -> PC_C`로 보이는 two-block-ahead 성격
> - BPU s1 current block(PC_B) 기준: `PC_B -> PC_C` 예측 (non-lookahead)

**AddrField 비트 구조** (`abtb/Helpers.scala:24`):
```
PC bit : [ 0  ] [ 2:1 ] [ 7:3  ] [ VAddrBits-1:8 ]
field  :  inst   bank    setIdx      unused
          Off   (2 bit)  (5 bit)

extraField:
  tag         = PC[24:1]   (start=instOffsetBits=1, width=24)
  targetLower = PC[22:1]   (start=instOffsetBits=1, width=22)
```

| field | PC bits | 비고 |
|---|---|---|
| instOffset | PC[0:0] | 1 bit |
| bankIdx | **PC[2:1]** | 2 bits → 4 banks 선택 |
| setIdx | **PC[7:3]** | 5 bits → 32 sets/bank 선택 |
| tag | **PC[24:1]** | 24 bits (bankIdx+setIdx 비트 포함) |

**[핵심] ABTB의 tag는 fetch block PC** (`abtb/Bundles.scala:85`):
```
AheadBtbEntry.tag = PC[24:1]   ← 개별 분기 PC가 아닌 fetch block start PC
```
> 일반 set-associative BTB는 tag = 개별 branch instruction PC → lookup 당 최대 1 way hit.
> ABTB는 tag = fetch block start PC → **같은 fetch block 내 여러 분기**가 각 way에 저장.
> 따라서 같은 fetch block을 lookup하면 **여러 way가 동시에 hit**할 수 있음 (정상 동작).
> Way들은 서로 다른 `position`(fetch block 내 분기 위치)으로 구분됨.

**Lookup 방식** (`abtb/AheadBtb.scala:107-178`):

> **주의**: ABTB는 SRAM index와 tag 비교에 서로 다른 PC를 사용한다 (ahead 메커니즘).

**[핵심] 왜 set-index(PC_A)와 tag(PC_B)가 서로 다른 PC인가?**

일반 BTB에서 set-index와 tag는 같은 PC에서 유래한다. ABTB는 다르다:

| 역할 | 일반 BTB | ABTB |
|------|----------|------|
| set-index | PC_B[7:3] → SRAM 어느 row? | **PC_A[7:3]** → SRAM 어느 row? (1 cycle 앞서 읽기) |
| tag | PC_B[24:1] → "이 entry가 PC_B를 위한 것?" | **PC_B[24:1]** → "이 entry가 PC_B를 위한 것?" |
| entry data | PC_B 안의 분기 정보 | PC_B 안의 분기 정보 |

- **set-index(PC_A)**: 순수 물리 주소 — "SRAM의 어느 row를 읽을까?" SRAM 2-cycle latency를 1 cycle 앞서 소모하기 위해 PC_A 사용.
- **tag(PC_B)**: 논리적 식별자 — "읽어온 entry들 중 어느 것이 PC_B 블록을 위한 것인가?" 소속 확인용.

**만약 tag를 PC_A로 사용한다면?**
- `entry.tag === getTag(PC_A)` → "이 entry가 PC_A 블록의 분기를 기록한 것인가?" 를 검색
- 그런 entry는 **저장된 적도 없다** (학습 시 `tag = getTag(PC_B)`로 저장하기 때문)
- PC_A 블록의 분기는 이미 훨씬 이전 cycle에 처리됨 — 지금 필요한 정보는 PC_B 블록의 분기

**ABTB entry의 의미**: "PC_A→PC_B 경로를 실행 중이었을 때, PC_B 블록 안의 어떤 분기(position)가 어디로(target) 점프하는가"
- `setIndex(PC_A)`: SRAM 물리 위치 (ahead 접근을 위한 index, next block으로 넘어가는 시점에 저장)
- `tag = getTag(PC_B)`: 이 entry가 PC_B 블록 소속임을 나타내는 논리 식별자
- `data(position, target)`: PC_B 안의 실제 분기 정보 → 다음 target PC = PC_C

```
[S0] SRAM 읽기 (이전 블록 PC_prev 기준):
  BankIndex = PC_prev[2:1]   → 해당 bank 선택  (s0_previousStartPc = io.startPc at cycle N)
  SetIndex  = PC_prev[7:3]   → set 선택 (32 sets)
  s0_previousStartPc = io.startPc  ← 변수명에 "previous" 포함: 다음 cycle의 "이전" 블록

[S1] SRAM 응답 대기 + 현재 BPU S0 PC 캡처:
  s1_startPc = io.startPc   ← 등록(RegEnable) 하지 않음! LIVE 값 (= 현재 BPU S0 PC = PC_curr)
  s1_entries = SRAM 응답 (setIndex(PC_prev) 기준)

[S2] 태그 비교 (현재 블록 PC_curr 기준):
  s2_startPc = RegEnable(s1_startPc) = PC_curr   ← SRAM 읽기에 사용된 PC와 다른 PC!
  s2_tag     = getTag(s2_startPc) = PC_curr[24:1]

  // 8 ways 동시 tag 비교 → hitMask 생성
  s2_hitMask[i] = s2_entries[i].valid && s2_entries[i].tag === s2_tag
  //   entries: setIndex(PC_prev)에서 읽은 값
  //   tag:     PC_curr[24:1]
  //   → 이전에 (setIndex(PC_prev), tag=PC_curr)로 저장된 entry가 있으면 HIT

  // hitMask가 여러 bit 켜질 수 있음 — PC_curr 블록의 여러 분기가 각 way에 저장
  // 예) PC_curr 블록에 branch@pos3, branch@pos7, branch@pos12가 있으면
  //     way0(tag=PC_curr, pos=3), way1(tag=PC_curr, pos=7), way2(tag=PC_curr, pos=12) 모두 hit

  // 각 hit way별로 예측 출력 (AheadBtb.scala:172-178)
  io.prediction[i].valid       := s2_valid && s2_hitMask[i]
  io.prediction[i].taken       := takenCounter[bankIdx][setIdx][i].isPositive
  io.prediction[i].cfiPosition := s2_entries[i].position
  io.prediction[i].target      := reconstruct(s2_startPc, s2_entries[i])
  // → 최대 8개 Valid[Prediction] 동시 출력 (hit된 way 수만큼 valid=true)
  // → BPU가 PC_curr의 S1에서 이 결과를 수신 (ahead 덕분에 적시 도착)
```

> **예외 처리** (`s2_multiHit`): 같은 position을 가진 두 way가 동시 hit → 이는 anomaly.
> 정상 케이스는 hit된 way들이 모두 서로 다른 position을 가짐 (`predict_hit_entry_num` perf counter).

**Entry Contents** (`abtb/Bundles.scala:83`):
```
AheadBtbEntry {                           // 1 entry = 1개 분기 (fetch block 내 하나의 branch)
  valid:           Bool
  tag:             UInt(24)               // fetch block start PC[24:1] (개별 분기 PC 아님!)
  position:        UInt(5)               // CfiPosition: fetch block 내 분기 위치 (way 간 구분자)
  attribute:       BranchAttribute(4)    // branchType(2) + rasAction(2)
  targetLowerBits: UInt(22)              // partial target: branch target PC[22:1]
  // targetCarry: 미포함 (EnableTargetFix=false)
}

// 8 ways × 32 sets × 4 banks = 1,024 entries 총합
// 같은 set 내 여러 way가 동일 tag(= 동일 fetch block PC)를 가질 수 있음
// → 한 fetch block에 대해 최대 8개 분기를 캐시 가능
```

**[핵심] 8 ways의 의미 — Branch Tree가 아닌 Fetch Block 내 복수 Branch**

8 ways는 "다음 fetch block(PC_B)이 가질 수 있는 branch instruction의 최대 개수"를 나타낸다.
이는 multi-level branch tree(경로 분기 트리)가 **아니다** — 단일 fetch block 내 복수 branch이다.

```
setIndex(PC_A)로 읽은 SRAM row:

  way0: {tag=PC_B, position=2,  target=PC_C1}  ← PC_B 블록 offset 2번째 instruction이 branch
  way1: {tag=PC_B, position=5,  target=PC_C2}  ← PC_B 블록 offset 5번째 instruction이 branch
  way2: {tag=PC_B, position=11, target=PC_C3}  ← PC_B 블록 offset 11번째 instruction이 branch
  way3: {tag=PC_K, position=7,  target=PC_C4}  ← 다른 경로 PC_A→PC_K의 branch (다른 tag)
  ...

  // 같은 tag(=PC_B) → 같은 fetch block 내 여러 branch → 동시 hit 정상
  // 다른 tag(=PC_K) → 다른 경로(PC_A→PC_K)의 entry → PC_K fetch 시에만 hit
```

8 ways의 두 가지 역할:

| 역할 | 설명 |
|------|------|
| **같은 tag, 다른 position** | PC_B 블록 내 여러 branch instruction 동시 캐시 (최대 8개) |
| **다른 tag** | setIndex(PC_A)를 공유하는 다른 경로(PC_A→PC_K 등)의 entry 공존 |

**오해 방지**: "PC_A → PC_B(2가지 분기) → 8가지 final destination"처럼 branch tree를 캐시하는 것이 아니다.
ABTB는 **단일 다음 fetch block(PC_B) 안에 branch가 여러 개 있을 수 있다**는 사실을 커버한다.

**Next PC 결정** (Bpu.scala에서 처리):
```
// ABTB에서 최대 8개 Valid[Prediction] 수신 (PC_B 블록 내 각 branch instruction 하나씩)
// uBTB의 1개 출력과 concat → s1_btbPrediction[0..8] (총 최대 9개)
// CompareMatrix: taken인 것 중 최소 position 선택 → 단일 s1_prediction

// 예) way0(pos=2, taken), way1(pos=5, not-taken), way2(pos=11, taken)
//     → taken인 것: way0, way2 중 min position = way0(pos=2)
//     → next PC = PC_C1
//
// 이유: fetch block 안에서 position이 작은 branch가 먼저 실행됨
//       → 그 branch가 taken이면 이후 instruction은 실행되지 않음
//       → "첫 번째 taken branch"의 target이 next fetch PC
```

**특징**: fetch block 내 최대 8개 분기를 동시에 캐시하고 한 번의 lookup에서 모두 반환.
CompareMatrix가 "가장 먼저 나오는 taken branch" 하나를 골라 next PC를 결정한다.

**페어 방향 예측기**: `uTAGE` (uBTB와 공유, S1 동시 결과)

**Fast Training**: `BpuFastTrain` (s3_valid 신호) → S3 결과로 빠른 학습

---

### [핵심] ABTB의 "Ahead" 파이프라인 메커니즘

#### 두 예측기가 같은 BPU S1에 도달하는 경로 비교

uBTB와 ABTB는 **서로 다른 사이클에 서로 다른 PC를 인덱스**하지만, 결과적으로 **같은 BPU S1 for PC_B**에 동시에 출력된다.

```
Cycle N:   BPU S0 = PC_A
  uBTB:  레지스터 비교 (combinational) at PC_A
  ABTB:  SRAM 읽기 시작 at setIndex(PC_A)          ← 물리 SRAM, 1 cycle 소요

Cycle N+1: BPU S1 = PC_A   (→ uBTB 출력으로 다음 s0_startPc = PC_B 결정)
  uBTB → PC_A 예측 출력: target = PC_B             ← 1-cycle delay from PC_A
  uBTB: 새로 PC_B로 레지스터 비교 시작
  ABTB:  SRAM 응답 도착, tag 비교 시작 (s1_startPc = PC_B live)

Cycle N+2: BPU S1 = PC_B
  uBTB → PC_B 예측 출력: target = PC_C             ← 1-cycle delay from PC_B (N+1)
  ABTB → PC_B 예측 출력: target = PC_C             ← 2-cycle delay from PC_A (N)
           ↑ 둘 다 BPU S1 for PC_B에서 동시 도착!
  CompareMatrix → 최소 cfiPosition 선택 → s1_prediction → next s0_startPc = PC_C
```

**핵심 정리:**
- uBTB: **1-cycle** (레지스터 기반, PC_B에서 출발)
- ABTB: **2-cycle** (물리 SRAM 기반, PC_A에서 출발) — ahead 트릭이 1-cycle을 당김
- 같은 ABTB도 시점을 `PC_B`(BPU s1 current block)로 잡으면 결과까지 **1-cycle**로 보인다
- 두 경로가 같은 목적지(BPU S1 for PC_B)에 도달

#### 왜 ABTB가 훨씬 큰데 같은 사이클에 출력할 수 있나

| | uBTB | ABTB |
|---|---|---|
| 저장 방식 | **레지스터** (CAM/FF) | **물리 SRAM** |
| 주어진 pipeline 사이클 | **1-cycle** | **2-cycle** (ahead 트릭) |
| Cycle N: | PC_A 즉시 비교 (combinational) | setIndex(PC_A) SRAM 읽기 |
| Cycle N+1: | PC_B 즉시 비교 (combinational) | SRAM 응답 + tag 비교 (s1_startPc=PC_B) |
| Cycle N+2: | PC_B 결과 출력 | PC_B 결과 출력 |

uBTB는 레지스터 기반이라 1-cycle 비교만으로 충분하다. ABTB는 물리 SRAM이라 원래 2-cycle이 필요한데, **ahead 트릭이 SRAM 접근을 1-cycle 앞당겨** 2-cycle 파이프라인을 같은 BPU S1에 맞춘다. SRAM 크기가 훨씬 커도 2-cycle 예산이 주어지므로 타이밍을 충족할 수 있다.

#### entry 저장 방식

```
entry 저장:
  setIndex = setIndex(PC_A)   ← 이전 블록 PC로 SRAM 주소
  tag      = getTag(PC_B)     ← 현재(다음) 블록 PC
  data     = PC_B 내 taken 분기 정보 (position, target=PC_C, attribute)
```

#### 상세 파이프라인 타임라인

```
Cycle N:   BPU S0=PC_A. ABTB S0 fires.
             → SRAM 읽기 시작: setIndex(PC_A)
Cycle N+1: BPU S0=PC_B, S1=PC_A.
             ABTB S1 fires (s1_fire = s1_valid && predictReqValid).
             → s1_startPc = io.startPc = PC_B  ← LIVE 값, 등록 안 함!
             → s1_entries = SRAM 응답 from setIndex(PC_A)
Cycle N+2: BPU S0=PC_C, S1=PC_B.
             ABTB S2 fires (s2_fire = s2_valid && predictionSent=BPU.s1_fire).
             → s2_startPc = PC_B, s2_tag = getTag(PC_B)
             → hitMask: entries[setIndex(PC_A)] vs tag=getTag(PC_B)
             → io.prediction 출력
             BPU S1 for PC_B: uBTB + ABTB 동시 출력 → CompareMatrix → s1_prediction
             → s2_abtbMeta 캡처: setIdx=setIndex(PC_A), valid=true
Cycle N+3: BPU S2=PC_B. s3_abtbMeta = s2_abtbMeta, s3_startPc = PC_B.
Cycle N+4: BPU S3=PC_B. fastTrain fires:
             → fastTrain.startPc         = s3_startPc = PC_B
             → fastTrain.abtbMeta.setIdx = setIndex(PC_A)
             → fastTrain.finalPrediction = s3_prediction (PC_B의 최종 예측)
```

#### [핵심] ABTB Training 메커니즘

**ABTB는 fastTrain만 사용한다 — FTQ commit train path 없음**

`AheadBtb.scala:204-206`:
```scala
private val t0_train = io.fastTrain.get.bits
private val t0_fire  = io.enable && io.fastTrain.get.valid
                       && t0_train.finalPrediction.taken
                       && t0_train.abtbMeta.valid
```

ABTB에는 `io.train` (FTQ commit 경로) 소비 코드가 없다. `io.fastTrain`만 소비한다. 따라서:
- Training 시점: **BPU S3** (PC_B가 BPU S1에 진입한 후 ~3 cycle)
- **"PC_C가 commit될 때 train"이 아니다** — commit(~20~30 cycle 후)보다 훨씬 앞서 완료

**Training 타이밍**:
```
Cycle N   : BPU S0 = PC_A → ABTB SRAM 읽기 시작 (setIndex(PC_A))
Cycle N+1 : BPU S1 = PC_B → abtbMeta 캡처: setIdx=setIndex(PC_A)
Cycle N+2 : BPU S2 = PC_B → s2_abtbMeta → s3_abtbMeta
Cycle N+3 : BPU S3 = PC_B → fastTrain 발생 ← Training HERE
Cycle N+4 : t1_fire → takenCounter 업데이트 + SRAM write (필요 시)

commit:      Cycle N+20~30 ← ABTB는 이때 아무것도 하지 않음
```

**Training 데이터 출처** (`Bpu.scala:182-186`, `AheadBtb.scala:272-275`):

| SRAM 필드 | 값 | 출처 |
|---------|-----|------|
| setIdx | `setIndex(PC_A)` | `abtbMeta.setIdx` (BPU S1에서 캡처) |
| tag | `getTag(PC_B)` | `fastTrain.startPc = s3_startPc` |
| position | taken branch의 fetch block 내 위치 | `s3_prediction.cfiPosition` |
| target | PC_C | `s3_prediction.target` |

→ 결과: entry at `setIndex(PC_A)`, tag=`getTag(PC_B)`, data=PC_B의 taken 분기 정보 (target=PC_C)

**abtbMeta lifetime — 3 cycle, FTQ 저장 없음**:
```
// Bpu.scala:151: "abtb meta won't be sent to ftq, used for abtb fast train"

BPU S1 fire → s2_abtbMeta (레지스터)
BPU S2 fire → s3_abtbMeta (레지스터)
BPU S3      → fastTrain으로 소비 → 다음 값으로 덮어씌움
```

다른 predictor(MBTB, TAGE 등)의 meta는 FTQ entry에 저장되어 commit까지 보존되지만, abtbMeta는 BPU 내부 레지스터에만 3 cycle 존재하고 소멸한다. FTQ entry에 abtbMeta 필드가 없는 이유다.

**Training 내용 — 두 가지 업데이트** (`AheadBtb.scala:234-266`):

1. **takenCounter (레지스터, 즉시 업데이트)**:
```scala
needDecrease = updateThisSet && isCond && (!t1_trainTaken || (t1_trainTaken && posBefore))
// taken branch보다 position이 앞선 conditional branch → 실제로 not-taken이었음 → 감소

needIncrease = updateThisSet && isCond && t1_trainTaken && posEqual
// position이 일치하는 conditional branch → 실제 taken → 증가
```

2. **SRAM entry write (2가지 경우)**:
```
t1_needWriteNewEntry : 해당 branch가 ABTB에 없음 → victim way에 신규 할당
t1_needCorrectTarget : hit됐으나 indirect branch의 target lower bits 불일치 → target 수정
```

**Misprediction 시 동작**:
```
S3 예측 기반으로 training했는데 실제로 틀렸을 경우 (commit 시 redirect):
  → BPU가 correct PC부터 re-fetch 시작
  → 새 경로가 BPU S3를 통과할 때 correct info로 ABTB 재학습 (덮어씌움)
  ABTB는 "BPU S3 기준 best guess"로 빠르게 학습하고, 틀리면 나중에 교정한다.
```

**Prediction hit 조건** (동일 경로 PC_A → PC_B 재방문 시):
```
ABTB S0: SRAM read at setIndex(PC_A)
ABTB S1: s1_startPc = io.startPc = PC_B  ← LIVE
ABTB S2: tag 비교: entry.tag(=getTag(PC_B)) === getTag(PC_B) → HIT!
         → PC_B 내 taken 분기 정보 반환 → BPU S1 for PC_B에 전달
```

**논문의 "two-block ahead"와의 차이:**
| | 논문 two-block ahead | XiangShan ABTB |
|---|---|---|
| 인덱스 기준 | PC_A (현재 블록) | PC_A (이전 블록) |
| 태그 기준 | PC_C (두 블록 앞) | PC_B (한 블록 앞) |
| 예측 대상 | PC_C의 분기 | PC_B의 분기 |
| 목적 | double I-fetch 또는 파이프라인화 | SRAM latency hiding → S1 타이밍 유지 |

---

### 11.4 MBTB (Main BTB) — S2 Stage

| 항목 | 값 |
|---|---|
| BTB 방식 | **Block BTB (다중 분기)** — tag = 32B-aligned half-block PC[31:16], 여러 way가 동일 tag 공유, 2 AlignBanks × 4 ways = 최대 8개 분기 반환 |
| 구조 | **Set-Associative + 2-Level Banking** |
| Total Entries | **8,192** |
| Ways | **4** |
| Align Banks | **2** (64B fetch block → 32B half-block 2개로 분할) |
| Internal Banks | **4** (SRAM 전력 절감 / read-write conflict 분산) |
| Sets per SRAM | **256** (= 8192 / 4 ways / 4 ibanks / 2 abanks) |
| Tag Width | **16 bits** |
| Target Width | 20 bits (2B aligned) |
| Taken Counter | **2-bit bimodal** (base predictor) |
| Write Buffer | 4 entries |
| Replacer | **LRU** |
| History 사용 | **없음** |
| 예측 Latency | **2 cycle** |

**AddrField 비트 구조** (`mbtb/Helpers.scala:30`, `mbtb/Parameters.scala`):
```
PC bit : [ 4:0  ] [ 5 ] [ 7:6 ] [  15:8  ] [  31:16  ] [ VAddrBits-1:32 ]
field  :  align   aBank  iBank    setIdx       tag           unused
          Offset  (1b)   (2b)    (8 bits)    (16 bits)

extraField:
  replacerSetIdx = PC[13:6]   (FetchBlockSizeWidth=6 부터 8 bits)
  targetLower    = PC[20:1]   (instOffsetBits=1 부터 20 bits)
  position       = PC[5:1]    (instOffsetBits=1 부터 5 bits)
  cfiPosition    = PC[6:1]    (instOffsetBits=1 부터 6 bits)
```

| field | PC bits | 비고 |
|---|---|---|
| alignOffset | PC[4:0] | 5 bits, 32B 정렬 블록 내 offset |
| alignBankIdx | **PC[5:5]** | 1 bit → 2 align banks 선택 |
| internalBankIdx | **PC[7:6]** | 2 bits → 4 internal banks 선택 |
| setIdx | **PC[15:8]** | 8 bits → 256 sets/SRAM 선택 |
| tag | **PC[31:16]** | 16 bits → way 선택 (4-way 비교) |

**AlignBank vs InternalBank — 2-Level Banking 계층**:

MBTB는 두 가지 독립적인 목적의 banking을 중첩하여 사용한다.

```
MBTB (총 8192 entries)
├── AlignBank[0]    ← 64B fetch block의 하위 32B 구간 담당
│   ├── InternalBank[0]   ← PC[7:6]=00 entries (physical SRAM)
│   │   ├── entrySRAM × 4 ways
│   │   └── counterSRAM
│   ├── InternalBank[1]   ← PC[7:6]=01
│   ├── InternalBank[2]   ← PC[7:6]=10
│   └── InternalBank[3]   ← PC[7:6]=11
└── AlignBank[1]    ← 64B fetch block의 상위 32B 구간 담당
    └── InternalBank[0..3]
총 SRAM: 2 × 4 × (4 entrySRAMs + 1 counterSRAM) = 40개
```

**AlignBank (2개) — 64B fetch block의 다중 분기 커버** (`mbtb/Parameters.scala`):
```
NumAlignBanks = FetchBlockSize / FetchBlockAlignSize = 64B / 32B = 2
파라미터 주석: "Highest level banks, alignment restriction"
```
- **목적**: 64B fetch block에 걸쳐 있는 여러 분기를 커버하기 위해, fetch block을 2개의 32B half-block으로 나눠 병렬 lookup
  - MBTB의 tag 단위 = 32B half-block (PC[31:16] + alignBankIdx로 구분)
  - 한 fetch block의 앞 32B → AlignBank[A], 뒤 32B → AlignBank[B]에서 병렬 lookup
  - 각 AlignBank에서 4 ways → 총 8개 분기 예측 반환
- **VecRotate 동작** (`MainBtb.scala:76-84`):
  ```scala
  s0_startPcVec(0) = s0_startPc          // 현재 startPc가 속한 32B half-block
  s0_startPcVec(1) = aligned(s0_startPc + 32B)  // 그 다음 32B half-block
  → rotate by getAlignBankIndex(s0_startPc)
  // 결과: AlignBank[i]는 항상 자신의 alignIdx에 맞는 PC를 수신
  ```
- **startPc offset 필터링** (`MainBtbAlignBank.scala:166`):
  ```scala
  val hit = rawHit && e.position >= s2_alignedInstOffset && !s2_crossPage
  // startPc가 32B half-block 중간에 시작하면, startPc 이전의 분기 entry는 제외
  // (AlignBank[1] = 항상 aligned PC → alignedInstOffset=0 → 필터 없음)
  ```
- **커버리지 제약**: 파라미터 주석: *"can provide at most (banks-1)/banks × predict width"*
  - startPc가 32B half-block의 중간에 시작하면, 64B fetch가 세 번째 32B half-block으로 삐져나올 수 있음
  - 2 AlignBanks로는 그 세 번째 부분 미커버 → worst-case 50% 커버리지 보장

**InternalBank (4개) — SRAM 전력 절감 + Read-Write Conflict 분산** (`mbtb/Parameters.scala`):
```
NumInternalBanks = 4
파라미터 주석: "Lowest level banks, read-write conflicts and reduce SRAM power"
```
- **목적**: 물리 SRAM을 4분할 → 매 cycle에 1개 InternalBank만 활성화 → 전력 75% 절감
- **선택 방식** (`MainBtbAlignBank.scala`):
  ```scala
  s0_internalBankMask = UIntToOH(getInternalBankIndex(s0_startPc))
  // getInternalBankIndex = PC[7:6] (2 bits) → InternalBank 0~3 중 1개만 active
  ```
- **Conflict 분산**: lookup PC와 update PC의 PC[7:6]이 다르면 서로 다른 InternalBank → 충돌 없음
- **Conflict 처리**: 같은 InternalBank hit 시 WriteBuffer(4 entries)로 forwarding
- **물리 SRAM 구조** (`MainBtbInternalBank.scala`):
  ```scala
  entrySrams  = Seq.tabulate(NumWay)(wayIdx => SRAMTemplate(...))    // way별 별도 entry SRAM
  counterSram = SRAMTemplate(TakenCounter, set=NumSets, way=NumWay)  // counter 전용 SRAM
  ```

| | AlignBank | InternalBank |
|---|---|---|
| **개수** | 2 | 4 (per AlignBank) |
| **목적** | Cross-alignment 처리 | SRAM 전력 절감 + conflict 분산 |
| **활성화** | 매 cycle 모두 병렬 active | 매 cycle 1개만 active (PC[7:6] 기준) |
| **SRAM 보유** | 없음 (routing 계층) | 있음 (entrySRAM × 4 + counterSRAM) |

**Lookup 방식** (`mbtb/MainBtbAlignBank.scala`):
```
// AlignBank별 동작 (2개 병렬):
alignBankIdx    = PC[5]      → AlignBank 선택 (이미 MainBtb top에서 라우팅 완료)
internalBankIdx = PC[7:6]    → 4개 internal bank 중 1개 선택 (SRAM 활성화)
SetIndex        = PC[15:8]   → 256 sets 중 선택
Tag             = PC[31:16]  → 4 ways 전체와 동시 tag 비교

// ABTB와 동일: 같은 32B half-block의 여러 분기가 각 way에 저장
// → 여러 way가 동시에 tag match 가능 (multi-hit 정상 동작)
rawHit[i] = entry[i].valid && entry[i].tag == s2_tag
hit[i]    = rawHit[i] && entry[i].position >= alignedInstOffset && !crossPage

// position 조건:
//   AlignBank[0](startPc 포함): alignedInstOffset > 0 가능 → startPc 이전 분기 제외
//   AlignBank[1](다음 half-block): alignedInstOffset = 0 → 전체 포함

// 결과: 각 AlignBank에서 최대 4개 Valid[Prediction] 출력
//       → 2 AlignBanks flatten → 총 최대 8개 (ABTB와 동일 구조)
io.result = alignBanks.flatMap(_.io.read.resp.predictions)  // 총 8개
```

> PC[VAddrBits-1:32]는 tag에 미포함 → 물리 주소 상위 비트 aliasing 가능.
> 실질적으로 32-bit PC 범위(PC[31:0]) 내에서 충분한 구분 보장.

**Entry Contents** (`mbtb/Bundles.scala:35`):
```
// MBTB는 두 개의 분리된 SRAM을 사용 (entry SRAM + counter SRAM)

// [1] Entry SRAM (구조 정보):
MainBtbEntry {
  valid:           Bool
  tag:             UInt(16)               // PC[31:16], way 선택 비교용
  attribute:       BranchAttribute(4)     // branchType(2) + rasAction(2)
  position:        UInt(4)               // CfiAlignedPosition: alignBank 내 분기 위치
                                         // = CfiPosition(5) - alignBankIdx(1) → 4 bits
  targetCarry:     TargetCarry(2)        // Fit/Overflow/Underflow (항상 저장)
  targetLowerBits: UInt(20)             // partial target: branch target PC[20:1]
}

// [2] Counter SRAM (방향 정보, base predictor):
counters: Vec[4, SatCtr(2-bit)]         // 4 ways 각각의 taken/not-taken bimodal counter
                                         // → TAGE base predictor로 사용
                                         // TAGE/SC가 이를 override하여 최종 방향 결정
```

**Next PC 결정** (`mbtb/MainBtb.scala`, `Bpu.scala`):
```
// S2에서 8개 predictions 수신 (2 AlignBanks × 4 ways, ABTB와 동일 구조)
io.result = alignBanks.flatMap(_.io.read.resp.predictions)  // Vec[8, Valid[Prediction]]

// 각 prediction entry:
pred[i].valid       = rawHit && position >= alignedInstOffset && !crossPage
pred[i].cfiPosition = Cat(posHigherBits, entry.position)    // {alignBankIdx, 4-bit} = 5-bit
pred[i].target      = fullTarget 재구성 (targetCarry로 상위 비트 보정)
pred[i].taken       = counter.isPositive  // bimodal base predictor

// ABTB의 multi-hit과 동일: 같은 32B half-block의 여러 분기가 각 way에 저장됨
// → 여러 way가 동시에 hit 가능 (동일 tag, 다른 position)
// → 8개 중 valid한 것들이 Bpu top의 s2_mbtbResult에 전달

// 방향 결정 (Bpu.scala s2_condTakenMask, 각 entry별 독립 적용):
//   1순위: SC가 threshold 초과 → SC 방향
//   2순위: TAGE provider table hit → TAGE 방향
//   3순위: pred[i].taken (MBTB bimodal counter)
finalTaken[i] = Mux(sc.scUsed[i],       sc.scTakenMask[i],
                Mux(tage.useProvider[i], tage.providerPred[i],
                                         pred[i].taken))

// 최종 분기 선택: CompareMatrix → 최소 cfiPosition의 taken entry → 단일 s3_prediction
```

**페어 방향 예측기**: `TAGE` (8-table) + `SC` (Statistical Corrector)
- MBTB의 내장 2-bit counter = **TAGE base predictor** (fallback)
- TAGE full predictor가 provider table로 override

---

### 11.5 History 종류 — PHR vs GHR

> **핵심**: 이 구현에서 TAGE / uTAGE / ITTAGE / SC(path)는 모두 **PHR(Path History Register)**을 사용한다.
> 전통적 TAGE의 GHR(분기 방향 taken/not-taken 기록)과 다르다.

#### 11.5.1 PHR (Path History Register) — `bpu/history/phr/`

분기마다 `(PC, target)` 쌍을 15-bit hash로 요약하여 축적한다.

```
pathHash(pc, target) = Cat(PC[9:1], 4'b0000)  XOR  target[16:2]
                       ←─── 9 bits of PC ───→      ←─ 15 bits ─→
결과: 15 bits (PathHashWidth = 15)
```

PHR 갱신 방식: 분기마다 좌로 Shamt=2 bit shift 후 새 hash를 XOR
```
PHR_new = (PHR_old << 2) XOR pathHash(pc, target)
```
- PHR 최대 누적 길이: **397 bits** (TAGE Table 7의 histLen 기준)
- `FoldedHistory`: PHR의 긴 비트열을 N-bit로 XOR-접기(fold)하여 예측기에 공급

**XOR-Fold 알고리즘** (`computeFoldedHist`, `bpu/Helpers.scala:172`):
```
FoldPHR(N) = PHR을 N-bit 청크로 나눠 모두 XOR
예) PHR[397:0] → fold to 9 bits:
    chunk0 = PHR[8:0]
    chunk1 = PHR[17:9]
    ...
    chunk44 = PHR[396:387]  (9 bits, zero-padded)
    FoldPHR(9) = chunk0 XOR chunk1 XOR ... XOR chunk44
```

#### 11.5.2 GHR (Global History Register) — `bpu/history/commonhr/`

조건 분기의 taken/not-taken 결과를 비트 스트림으로 기록. S3 fire 시 갱신.

- **SC 전역 테이블(GlobalEnable)** 에서만 사용 예정이나 현재 **`GlobalEnable = false`** (비활성)
- **SC 역방향 테이블(BWEnable)** 도 마찬가지로 **`BWEnable = false`** (비활성)

#### 11.5.3 PC bit 추출 원칙 (AddrField)

`AddrField`는 bit 0부터 순차적으로 field를 할당한다 (`utils/AddrField.scala`):
```
extract(fieldName, pc) = pc[end:start]  (start/end는 순차 누적)
```

**공통 전제값** (XiangShan KMH v3, RVC 활성화):
- `instOffsetBits = 1` (2B 정렬, bit 0 = 항상 0)
- `FetchBlockAlignWidth = 5` (32B = FetchBlockSize/2)
- `VAddrBits = 50` (PrunedAddr 길이)

---

### 11.6 uTAGE (Micro TAGE) — Index/Tag Hashing 상세

uBTB / ABTB와 페어. S1 1-cycle 결과. **`MicroTageTable.scala:83`**

```
unhashedIdx = PC[VAddrBits-1 : 1]   (PC >> instOffsetBits)
unhashedTag = PC[VAddrBits-1 : 7]   (PC >> PCHighTagStart=7)
```

#### Table 0 (512 sets, histLen=9, histBitsInTag=9, tagLen=15)

| 항목 | FoldedLength | 공식 |
|---|---|---|
| idxFhInfo | min(9, 9) = **9 bits** | PHR[9-1:0] fold → 9 bits |
| tagFhInfo | min(9, 9) = **9 bits** | PHR fold → 9 bits |
| altTagFhInfo | min(9, 8) = **8 bits** | PHR fold → 8 bits |

```
SetIndex = (PC[9:1] XOR FoldPHR_9)[8:0]

lowTag   = (PC[VAddrBits-1:7] XOR FoldPHR_9 XOR (FoldPHR_8 << 1))[8:0]
highTag  = Cat(PC[16], PC[14], PC[12], PC[10], PC[8], PC[7], PC[6],  ← 11 bits
               PC[5],  PC[4],  PC[3],  PC[2])                          (non-consecutive 선택)
Tag      = Cat(highTag, lowTag)[14:0]                               ← 15 bits 截断
         = {highTag[5:0], lowTag[8:0]}
```

> highTag는 `PCTagHashBitsForShortHistory = [15,13,11,9,7,6,5,4,3,2,1]` 인덱스를 `unhashedIdx`에 적용.
> `unhashedIdx[i] = PC[i+1]` 이므로 실제 PC bit는 각 인덱스+1.

#### Table 1 (512 sets, histLen=16, histBitsInTag=12, tagLen=16)

| 항목 | FoldedLength | 공식 |
|---|---|---|
| idxFhInfo | min(16, 9) = **9 bits** | PHR fold → 9 bits |
| tagFhInfo | min(16, 12) = **12 bits** | PHR fold → 12 bits |
| altTagFhInfo | min(16, 11) = **11 bits** | PHR fold → 11 bits |

```
SetIndex = (PC[9:1] XOR FoldPHR_9)[8:0]

lowTag   = (PC[VAddrBits-1:7] XOR FoldPHR_12 XOR (FoldPHR_11 << 1))[11:0]
highTag  = Cat(PC[19], PC[17], PC[15], PC[13], PC[11],  ← 10 bits
               PC[7],  PC[6],  PC[5],  PC[3],  PC[2])   (PCTagHashBitsForMediumHistory)
Tag      = Cat(highTag, lowTag)[15:0]                   ← 16 bits 截断
         = {highTag[3:0], lowTag[11:0]}
```

- Counter: 3-bit signed, Useful: 2 bits
- **Fast Training**: S3 결과(`BpuFastTrain`)로 학습. 커밋 기반 학습 불가 (연속 예측 블록 없음)

---

### 11.7 TAGE (Main TAGE) — Index/Tag Hashing 상세

MBTB와 페어. S2 결과. **`tage/Helpers.scala:51`, `tage/TageTable.scala`**

**AddrField 구조** (8개 테이블 동일 구조, 4096 total / 4 banks / 2 ways = **512 sets**):
```
PC bit  : [ 0 ] [ 2:1 ] [ 11:3 ] [ 24:12 ] [ 49:25 ]
field   :  inst   bank   setIdx     tag      unused
          Offset  Idx    (9 bit)  (13 bit)
```

| field | PC bits | 비고 |
|---|---|---|
| instOffset | PC[0:0] | 1 bit, always 0 (RVC 2B 정렬) |
| bankIdx | PC[2:1] | 2 bits, 4 banks |
| setIdx | PC[11:3] | 9 bits, 512 sets/bank |
| tag | PC[24:12] | 13 bits |

```
// 8개 테이블 각각에 대해 3개의 FoldedHistory 생성
idxFhInfo    : FoldedLength = min(histLen, 9)   → FoldPHR_idx
tagFhInfo    : FoldedLength = min(histLen, 13)  → FoldPHR_tag
altTagFhInfo : FoldedLength = min(histLen, 12)  → FoldPHR_altTag

// tage/Helpers.scala:34
forIdx = FoldPHR_idx
forTag = FoldPHR_tag XOR Cat(FoldPHR_altTag, 0.U(1.W))
       = FoldPHR_tag XOR (FoldPHR_altTag << 1)   ← 13 bits

// tage/Helpers.scala:64-65
BankIndex = PC[2:1]                               ← SRAM bank 선택
SetIndex  = PC[11:3] XOR forIdx                   ← 9 bits

// tage/Helpers.scala:67-68
RawTag    = PC[24:12] XOR forTag                  ← 13 bits

// Tage.scala (tag matching 시 CfiPosition XOR)
FinalTag  = RawTag XOR CfiPosition                ← CfiPosition = fetch block 내 분기 위치
```

**histLen별 FoldPHR 비트 범위 예시:**

| Table | histLen | FoldPHR_idx (9 bit) | FoldPHR_tag (13 bit) |
|---|---|---|---|
| 0 | 4 | PHR[3:0] → fold4→pad9 | PHR[3:0] → fold4→pad13 |
| 3 | 29 | PHR[28:0] → fold9 | PHR[28:0] → fold13 |
| 7 | 397 | PHR[396:0] → fold9 | PHR[396:0] → fold13 |

> `fold4→pad9`: FoldedLength=4 < SetIdxWidth=9인 경우, uTAGE와 달리 TAGE는 단순 XOR 후 9-bit 취함.
> histLen이 짧으면 folded 결과는 하위 histLen bits만 유효하고 나머지 padding=0.

---

### 11.8 TAGE Base vs TAGE Full

| 구분 | TAGE Base | TAGE Full |
|---|---|---|
| 구현 | MBTB 내장 **2-bit bimodal counter** | 8개 tagged history table |
| 역할 | 아무 table도 hit 없을 때 **default 예측** | Provider/Alternate 로직으로 정교한 예측 |
| History 사용 | **없음** | PHR 최대 **397 bits** 경로 히스토리 |
| 선택 로직 | 단순 taken/not-taken | Provider(가장 긴 매칭 history) → 약하면 Alternate |
| Override 관계 | Base = fallback | Full이 base를 override |

**Provider 선택 로직**:
```
if (provider table hit && !(useAltOnNA && counter is weak)):
    prediction = provider.isTaken
else if (alternate table hit):
    prediction = alternate.isTaken
else:
    prediction = MBTB bimodal (base)
```

---

### 11.9 SC (Statistical Corrector) — 구현 상세

TAGE가 특정 방향으로 편향될 때, 통계적으로 감지하여 보정하는 예측기. **`sc/Sc.scala`, `sc/Helpers.scala`**

> **Loop 예측기 ("L" in TAGE-SC-L)**: 이 codebase에 **미구현**. `BPUCtrl`에 `loop_enable` 필드만 존재하며 실제 모듈 없음.

---

#### 11.9.1 테이블 구조

| Table | 활성 | Sets | histLen | WayIdx 기준 | SetIndex 공식 |
|---|---|---|---|---|---|
| PathTable[0] | **True** | 128 | 8 | cfiPos[2:0] | `(PC[11:5] XOR FoldPHR_7)[6:0]` |
| PathTable[1] | **True** | 128 | 16 | cfiPos[2:0] | `(PC[11:5] XOR FoldPHR_7)[6:0]` |
| GlobalTable[0] | False | 128 | 8 | — | `(PC[11:5] XOR FoldGHR_7)[6:0]` ← GlobalEnable=false |
| GlobalTable[1] | False | 128 | 16 | — | `(PC[11:5] XOR FoldGHR_7)[6:0]` ← 비활성 |
| BWTable[0] | False | 128 | 4 | — | `(PC[11:5] XOR FoldBW_7)[6:0]` ← BWEnable=false |
| BWTable[1] | False | 128 | 8 | — | `(PC[11:5] XOR FoldBW_7)[6:0]` ← 비활성 |
| BiasTable | **True** | 128 | — | `Cat(cfiPos[2:0], {tageweak,tagetaken})` | `PC[11:5][6:0]` |

```
공통 구조:
  NumWays   = NumBtbResultEntries = 8    ← MBTB result entries 수
  WayIdx    = cfiPosition[2:0]           ← fetch block 내 분기 위치 하위 3 bits
  BankIndex = PC[4:4]                    ← PC >> (1+3) 하위 1 bit
  Counter   = 6-bit signed saturating    ← ScEntry.ctr

  BiasTable WayIdx = Cat(cfiPos[2:0], tageweak(1), tagetaken(1))  ← 5 bits → 32 ways
    tageweak  = provider.valid && provider.ctr.isWeak
    tagetaken = provider.valid && provider.ctr.isPositive
    → "TAGE가 약하게 taken일 때의 편향"을 별도 셀에 저장
```

---

#### 11.9.2 핵심 수식 — percsum

```scala
// Helpers.scala:61
def getPercsum(ctr: SInt): SInt = Cat(ctr, 1.U(1.W)).asSInt
// 의미: percsum = 2 * ctr + 1  (7-bit signed)
```

| ctr (6-bit signed) | percsum |
|---|---|
| +31 (최대 포화) | **+63** (강한 taken) |
| +1 (약한 taken) | **+3** |
| 0 (중립) | **+1** (≠0: dead-zone 제거) |
| -1 (약한 not-taken) | **-1** |
| -32 (최대 포화) | **-63** (강한 not-taken) |

> LSB=1을 붙이는 이유: ctr=0 중립 상태에서 percsum=+1이 되어 합산 시 0이 되는 dead-zone이 없어짐.

---

#### 11.9.3 예측 파이프라인 (S1→S2)

```
S1 (SRAM read 완료):
  s1_sumPercsum[wayIdx] = Σ percsum(pathTable[k][wayIdx].ctr)   (k=0,1 활성만)

S2 (MBTB 결과 도착):
  biasWayIdx[i] = Cat(wayIdx, {tageweak, tagetaken})
  totalPercsum[i] = s1_sumPercsum[wayIdx] + biasPercsum[biasWayIdx]

  // TAGE provider confidence에 따라 effective threshold 결정
  threshold T = scThreshold[wayIdx].value >> 3   (초기 720 >> 3 = 90)

  TAGE 포화 (isSaturate):  effectiveThres = T >> 1 = 45
  TAGE 중간 (isMid):       effectiveThres = T >> 2 = 22
  TAGE 약함 (isWeak):      effectiveThres = T >> 3 = 11

  scUsed[i] = hit && tagePredValid && aboveThreshold(totalPercsum[i], effectiveThres)
  scPred[i] = (totalPercsum[i] >= 0)   ← SC 예측 방향

  직관: TAGE가 confident할수록 SC가 뒤집으려면 더 강한 신호 필요
```

```scala
// Helpers.scala:63
def aboveThreshold(scSum: SInt, threshold: UInt): Bool =
  (scSum > threshold.zext) && pos(scSum) ||
  (scSum < -threshold.zext) && neg(scSum)
```

---

#### 11.9.4 학습 (Train)

```
테이블 ctr 업데이트 조건:
  needUpdate = writeValid
             && tagePredValid
             && (scPred ≠ actual  ||  !sumAboveThres)
  → SC 틀렸거나, 합이 임계값 미만이면 학습 (임계값 초과 & 정답이면 이미 충분 → 학습 생략)
  업데이트 방향: ctr.getUpdate(actual_taken)

Adaptive Threshold 업데이트:
  shouldUpdate = (tagePred ≠ scPred) && (scWrong || !sumAboveThres)
  threshold.getUpdate(scWrong)
  → SC 틀리면 threshold++ (더 확실할 때만 사용)
  → SC 맞으면 threshold-- (더 적극적으로 사용)
  ← way별 독립 12-bit saturating counter
```

---

#### 11.9.5 동작 예시

**시나리오**: 루프 분기 `0x1080`이 4번 중 3번 taken, 1번 not-taken. TAGE는 항상 TAKEN 예측.

```
branch: cfiPosition=5,  wayIdx = 5[2:0] = 5

[ S2 예측 시점 ]
TAGE provider: ctr=+1 (양수, weak) → tageweak=1, tagetaken=1
biasWayIdx = Cat(5, 0b11) = 0b10_1011 = 43

PathTable[histLen=8 ]: ctr = -10 → percsum = -19
PathTable[histLen=16]: ctr = -15 → percsum = -29
  sumPercsum[5] = -48

BiasTable[43]:  ctr = -8  → biasPercsum = -15

totalPercsum = -48 + (-15) = -63

TAGE weak → effectiveThreshold = 90 >> 3 = 11
|−63| = 63 > 11 → scUsed = true
scPred = (−63 >= 0) = NOT_TAKEN   ← TAGE(TAKEN) override!
→ 최종 예측: NOT_TAKEN ← 정답

[ 학습 시점: actual = NOT_TAKEN ]
  needUpdate = true && true && (NOT_TAKEN ≠ NOT_TAKEN || !sumAboveThres)
             = true && (false || !(63 > 90)) = true && true = true
  → ctr 업데이트: NOT_TAKEN 방향으로 증가

  shouldUpdateThres = (tagePred=TAKEN ≠ scPred=NOT_TAKEN) && (scWrong=false || ...)
                    = true && (false || !(63>90)) = true && true = true
  threshold.getUpdate(scWrong=false) → threshold--
  → 다음에는 SC를 더 쉽게 사용
```

---

#### 11.9.6 현재 구현 상태

| 컴포넌트 | 상태 |
|---|---|
| PathTable (histLen=8,16) | **활성** (PHR 기반) |
| GlobalTable (histLen=8,16) | **비활성** (GlobalEnable=false, GHR 미활성) |
| BWTable (histLen=4,8) | **비활성** (BWEnable=false) |
| BiasTable | **활성** (PC + TAGE 결과 인덱스) |
| Adaptive Threshold | **활성** (way별 독립 12-bit counter, 초기값 720) |
| Loop 예측기 ("L") | **미구현** (BPUCtrl.loop_enable 필드만 존재) |

---

### 11.10 ITTAGE (Indirect Target TAGE) — Index/Tag Hashing 상세

간접 분기 target 예측. **`ittage/IttageTable.scala:94`**

```
unhashedIdx = PC >> instOffsetBits = PC[VAddrBits-1 : 1]
NumBanks = 2, bankIdxWidth = 1
```

| Table | nRows | histLen | setsPerBank | setIdxWidth | bankIdx | setIdxBase | setIdx | tagBase |
|---|---|---|---|---|---|---|---|---|
| 0 | 256 | 4 | 128 | 7 | PC[1] | PC[8:2] | `(PC[8:2] XOR FoldPHR_4)[6:0]` | PC[VAddrBits-1:9] |
| 1 | 256 | 8 | 128 | 7 | PC[1] | PC[8:2] | `(PC[8:2] XOR FoldPHR_7)[6:0]` | PC[VAddrBits-1:9] |
| 2 | 512 | 13 | 256 | 8 | PC[1] | PC[9:2] | `(PC[9:2] XOR FoldPHR_8)[7:0]` | PC[VAddrBits-1:10] |
| 3 | 512 | 16 | 256 | 8 | PC[1] | PC[9:2] | `(PC[9:2] XOR FoldPHR_8)[7:0]` | PC[VAddrBits-1:10] |
| 4 | 512 | 32 | 256 | 8 | PC[1] | PC[9:2] | `(PC[9:2] XOR FoldPHR_8)[7:0]` | PC[VAddrBits-1:10] |

**Tag 공식** (모든 테이블 동일 구조):
```
tagFhInfo    : FoldedLength = min(histLen, 9)   → FoldPHR_tag
altTagFhInfo : FoldedLength = min(histLen, 8)   → FoldPHR_altTag

Tag = (tagBase XOR FoldPHR_tag XOR (FoldPHR_altTag << 1))[8:0]   ← 9 bits

예) Table 4 (histLen=32):
    Tag = (PC[VAddrBits-1:10] XOR FoldPHR_9 XOR (FoldPHR_8 << 1))[8:0]
```

- Region 기반 target 압축: 16 regions, 2 ports, PLRU replacer
- TargetOffset: 20 bits (region 내 offset)

---

### 11.11 PC Bit 사용 비교표 (전체 요약)

| 예측기 | history 종류 | index용 PC bits | tag용 PC bits |
|---|---|---|---|
| **uBTB** | 없음 | — (fully assoc) | PC 전체 tag 직접 비교 (22 bits) |
| **ABTB** | 없음 | PC[7:3] (5 bits) | PC[24:1] (24 bits) |
| **MBTB** | 없음 | PC[15:8] (8 bits) | PC[31:16] (16 bits) |
| **uTAGE T0** | PHR (path) | PC[9:1] XOR FoldPHR_9 | lowTag: PC[VAddrBits-1:7] XOR PHR; highTag: PC 선택 비트 concat |
| **uTAGE T1** | PHR (path) | PC[9:1] XOR FoldPHR_9 | lowTag: PC[VAddrBits-1:7] XOR PHR; highTag: PC 선택 비트 concat |
| **TAGE (all)** | PHR (path) | PC[11:3] XOR FoldPHR_idx | PC[24:12] XOR forTag XOR CfiPos |
| **SC path** | PHR (path) | PC[11:5] XOR FoldPHR_7 | 없음 (tagless) |
| **ITTAGE T0~1** | PHR (path) | PC[8:2] XOR FoldPHR | PC[VAddrBits-1:9] XOR FoldPHR |
| **ITTAGE T2~4** | PHR (path) | PC[9:2] XOR FoldPHR | PC[VAddrBits-1:10] XOR FoldPHR |

---

### 11.12 RAS (Return Address Stack) — S3 Stage

| 구분 | uRAS (Micro RAS) | RAS (Full) |
|---|---|---|
| Stage | **S1** (fast speculative) | **S3** (commit-based) |
| Stack Size | — | Commit stack **16** + Speculative queue **32** |
| Stack Counter | — | 3-bit (merged call 표현, 최대 7) |
| 용도 | ret target 빠른 제공 | 정확한 ret 주소 복원 |

---

### 11.13 History별 최대 길이

| History 종류 | 최대 길이 | 사용 예측기 |
|---|---|---|
| PHR (Path: PC+target hash) | **397 bits** (TAGE Table 7) | TAGE, uTAGE, ITTAGE, SC(path) |
| GHR (Global: taken/not-taken) | 16 bits | SC(global) ← 현재 비활성 |
| Backward History | 8 bits | SC(backward) ← 현재 비활성 |
| PHR per-branch hash window | **15 bits** | pathHash = Cat(PC[9:1],0000) XOR target[16:2] |

---

### 11.14 FastTrain 메커니즘

#### 11.14.1 왜 FastTrain이 별도로 필요한가

일반 `train` 신호는 Backend → ROB commit → FTQ → BPU 경로를 거쳐 **20~30 사이클** 지연 후 도착한다. S1 predictor들(uBTB, ABTB, uTAGE)은 BPU의 가장 빠른 예측 경로를 담당하기 때문에, 이들을 지연 없이 갱신하기 위해 **BPU 내부 S3 결과를 즉시 학습 신호로 변환**한 것이 FastTrain이다.

```
일반 Train:  BPU → FTQ → ROB 실행 → FTQ → BPU     (~20~30 cycles)
FastTrain:   BPU S3 → BPU S1 predictor              (~2~3 cycles)
```

`uTAGE`의 경우 "연속된 예측 블록이 resolve 시점에 없다"는 이유로 FastTrain이 **필수**로 설정되어 있다 (`EnableFastTrain = true`, `utage/Parameters.scala`).

#### 11.14.2 BpuFastTrain 번들 구조 (`bpu/Bundles.scala`)

주석: `// use s3 prediction to train s1 predictors`

```
BpuFastTrain {
  startPc:         PrunedAddr     ← S3 블록 PC (ABTB 분석 기준: PC_B)
  finalPrediction: Prediction {   ← S3 최종 예측
    taken:       Bool
    cfiPosition: UInt(5)
    target:      PrunedAddr
    attribute:   BranchAttribute
  }
  hasOverride:  Bool              ← S3가 S1 예측을 override했는지
  abtbMeta:     AheadBtbMeta {   ← ABTB S2 출력 메타 (FTQ 비전달, fastTrain 전용)
    valid:    Bool
    setIdx:   UInt                ← setIndex(PC_A): SRAM은 이전 블록으로 읽음 (ahead)
    bankMask: UInt
    entries:  Vec[8, {
      hit:             Bool       ← S2에서 해당 way hit 여부
      attribute:       BranchAttribute
      position:        UInt(5)
      targetLowerBits: UInt(22)
    }]
  }
  utageMeta:    MicroTageMeta {   ← uTAGE S1 출력 메타
    histTableHitMap:         Vec[2, Bool]  ← 각 uTAGE 테이블 hit 여부
    histTableTakenMap:       Vec[2, Bool]  ← 각 테이블의 taken 예측
    histTableUsefulVec:      Vec[2, UInt]  ← 유용함 카운터
    histTableCfiPositionVec: Vec[2, UInt]  ← 각 테이블의 CFI 위치
    baseTaken:               Bool          ← ABTB 기반 예측 방향
    baseCfiPosition:         UInt
  }
}
```

#### 11.14.3 생성 및 전달 (`Bpu.scala:182-195`)

```scala
fastTrain.valid                := s3_valid
fastTrain.bits.startPc         := s3_startPc          // PC_B
fastTrain.bits.finalPrediction := s3_prediction        // S3 최종 예측
fastTrain.bits.abtbMeta        := s3_abtbMeta          // BPU S1(PC_B) fire 시 캡처된 ABTB 메타
fastTrain.bits.utageMeta       := s3_utageMeta
fastTrain.bits.hasOverride     := s3_override

predictors.foreach { p =>
  p.io.fastTrain.foreach(_ := fastTrain)  // HasFastTrainIO 상속한 predictor에만 전달
}
```

**abtbMeta 캡처 경로** (`Bpu.scala:151-153`):
```scala
// abtb meta won't be sent to ftq, used for abtb fast train
private val s2_abtbMeta = RegEnable(abtb.io.meta, s1_fire)  // BPU S1(PC_B) fire 시 캡처
private val s3_abtbMeta = RegEnable(s2_abtbMeta, s2_fire)
// → s3_abtbMeta.setIdx = setIndex(PC_A): ahead 인덱싱 보존
```

> **핵심**: `abtbMeta`는 FTQ로 전송되지 않는다. BPU 내부에서만 보존 → fastTrain으로 ABTB에 피드백.

#### 11.14.4 소비자 predictor 및 t0_fire 조건

| Predictor | 활성화 | t0_fire 조건 | 비고 |
|---|---|---|---|
| **ABTB** (`abtb/AheadBtb.scala:206`) | 항상 | `enable && valid && taken && abtbMeta.valid` | taken일 때, meta 유효할 때만 |
| **uBTB** (`ubtb/MicroBtb.scala:103`) | `UseFastTrain=true` (기본값) | `enable && valid` | taken 조건 없음; false 시 mispred train으로 대체 |
| **uTAGE** (`utage/MicroTage.scala:127`) | `EnableFastTrain=true` (필수) | `enable && valid` | 조건부 분기만 내부 필터 |

#### 11.14.5 ABTB fastTrain 학습 로직

**t0_fire 조건 해석:**
```
enable                         — ABTB 활성화
&& fastTrain.valid             — BPU S3 스테이지 valid
&& finalPrediction.taken       — S3 최종 예측이 taken
                                 (fallthrough = not-taken은 기본값 → 학습 불필요)
&& abtbMeta.valid              — S1에서 ABTB가 실제로 예측을 출력했는지
                                 (ABTB miss → meta.valid=false → 스킵)
```

**T1 학습 동작** (`abtb/AheadBtb.scala:217-304`):
```
t1_setIdx   = abtbMeta.setIdx   = setIndex(PC_A)   ← ahead 인덱스 그대로 사용
t1_bankMask = abtbMeta.bankMask

[taken counter 업데이트] (조건부 분기 entry별)
  position < trainPos  → counter 감소  (해당 분기는 실제로 taken이 아님)
  position == trainPos → counter 증가  (실제 taken 분기와 일치)

[entry 쓰기]
  !t1_hit:                          → 새 entry 할당
    tag = getTag(fastTrain.startPc) = getTag(PC_B)  ← ahead 태그
    stored at setIndex(PC_A)

  t1_hit && isIndirect && targetDiff → target 수정 (tag/position 유지)
```

#### 11.14.6 uBTB fastTrain 학습 로직

uBTB는 always-taken predictor이므로 **taken 여부 무관하게** 학습한다:

```
t0_fire = fastTrain.valid && enable  (taken 조건 없음)

T1 케이스별:
  !hit:                            → victim entry 교체 (useful=0 선택)
  hit, 불일치 or !actualTaken:     → useful counter 감소 or entry 재초기화
  hit, 모두 일치, actualTaken:     → useful counter 증가 (entry 강화)
```

**FastTrain=false 시 동작** (`ubtb/Parameters.scala`: `UseFastTrain=false`로 변경 가능):
```scala
// 일반 train으로 대체 (misprediction 시에만)
t0_fire = io.stageCtrl.t0_fire && io.train.mispredictBranch.valid && io.enable
```

#### 11.14.7 uTAGE fastTrain 학습 로직

uTAGE는 조건부 분기 방향만 처리하며, **misprediction 감지 후** 학습한다:

```
t0_fire = fastTrain.valid && enable

misprediction 판정:
  t0_histHitMisPred     = uTAGE hit 했으나 (방향 또는 cfiPosition) 예측 틀림
  t0_histMissHitMisPred = uTAGE miss + S3이 override해서 taken 예측

학습 결정:
  needAlloc  = mispred      → 새 entry 할당 (useful=0 피해자에 덮어씀)
  needUpdate = hit          → counter 업데이트

useful counter:
  증가: 정확 예측 && ABTB(base predictor)가 틀림  ← uTAGE가 correction에 기여
  감소: misprediction
```

#### 11.14.8 일반 Train vs FastTrain 비교

| 항목 | 일반 Train (`BpuTrain`) | FastTrain (`BpuFastTrain`) |
|---|---|---|
| **출처** | Backend → FTQ → BPU | BPU 내부 S3 |
| **지연** | ~20~30 cycles | ~2~3 cycles |
| **정확성** | 실제 실행 결과 (ground truth) | S3 예측 (추정값) |
| **대상** | 모든 predictor | S1 predictor만 (uBTB, ABTB, uTAGE) |
| **빈도** | mispred + commit 시 | 매 S3 valid 사이클 |
| **메타** | BpuResolveMeta (FTQ 저장 후 반환) | abtbMeta + utageMeta (BPU 내부 보존) |
