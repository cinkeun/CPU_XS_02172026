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

```
S0 (combinational)
  ├─ npcGen: PhyPriorityMuxGenerator → 다음 PC 결정
  ├─ foldedGhGen: folded history 계산
  ├─ s0_pc_dup[4]: PC 복제본 (numDup=4)
  └─ predictors.io.in.valid 구동

S1 (REG: s1_valid_dup, s1_pc_dup = RegEnable(s0_pc, s0_fire))
  ├─ FauFTB (fast path): uFTB lookup → s1 prediction
  ├─ s1_fire → bpu_to_ftq.resp (s1 결과 전달)
  └─ s1 redirect 발생 시 → overrideBubble(0)

S2 (REG: s2_valid_dup, s2_pc_dup = SegmentedAddrNext(s1_pc, s1_fire))
  ├─ Main FTB lookup → FTBEntry 결과
  ├─ TAGE base 조회
  ├─ s2_fire → bpu_to_ftq.resp (s2 결과 전달, hasRedirect=true 가능)
  └─ s2 redirect 발생 시 → overrideBubble(1)

S3 (REG: s3_valid_dup, s3_pc_dup = SegmentedAddrNext(s2_pc, s2_fire))
  ├─ TAGE full (다수 테이블) 결과 적용
  ├─ SC (Statistical Corrector) 보정
  ├─ ITTAGE (간접 target) 적용
  ├─ RAS (Return Address Stack) 적용
  ├─ s3_fire → bpu_to_ftq.resp (최종 결과, 가장 높은 우선순위)
  └─ FTQ stall 시 → ftqFullStall, s3_ready=false
```

### 4.2 전역 이력 레지스터 (GHR)

- `val ghv = RegInit(0.U.asTypeOf(Vec(HistoryLength, Bool())))` — circular history vector
- `val ghv_wire = WireInit(ghv)` — combinational write 지원
- `ghvBitWriteGens`: HistoryLength 개 PhyPriorityMuxGenerator — 우선순위 write

### 4.3 Folded History

- `AllFoldedHistories`: 다수의 `FoldedHistory(len, compLen, numBr)` 모음
- `AllAheadFoldedHistoryOldestBits`: 다음 사이클 folded history 사전 계산용 (타이밍 최적화)
- S0에서 speculative update, redirect 시 복원

### 4.4 FTQ 전달 응답 구조 (BranchPredictionResp)

```
BranchPredictionResp
  ├─ s1: BranchPredictionBundle (isNotS3=true)
  ├─ s2: BranchPredictionBundle (isNotS3=true)
  ├─ s3: BranchPredictionBundle (isNotS3=false)
  ├─ last_stage_meta: UInt(MaxMetaLength.W)
  ├─ last_stage_spec_info: Ftq_Redirect_SRAMEntry
  └─ last_stage_ftb_entry: FTBEntry

BranchPredictionBundle
  ├─ pc: Vec(numDup, UInt)
  ├─ valid: Vec(numDup, Bool)
  ├─ hasRedirect: Vec(numDup, Bool)
  ├─ full_pred: Vec(numDup, FullBranchPrediction)
  └─ ftq_idx: FtqPtr

FullBranchPrediction
  ├─ br_taken_mask: Vec(numBr, Bool)
  ├─ slot_valids: Vec(totalSlot, Bool)
  ├─ targets: Vec(totalSlot, UInt) — 각 slot의 taken target
  ├─ jalr_target: UInt — 간접 분기 target (ITTAGE)
  ├─ offsets: Vec(totalSlot, UInt) — fetch block 내 offset
  ├─ fallThroughAddr: UInt
  ├─ is_jal/jalr/call/ret: Bool
  └─ hit: Bool (FTB hit 여부)
```

---

## 5. Functionality

### 5.1 PC 생성 (S0)

- `npcGen_dup`: 우선순위 MUX (높은 우선순위부터): redirect > s3_pred > s2_pred > s1_pred > seq(+fetchWidth)
- `redirect.valid` 수신 시 즉시 redirect target을 S0 PC로 사용

### 5.2 FauFTB 예측 (S1)

- uFTB (micro-FTB): 작은 fully-associative FTB로 S1에 결과 제공
- `s1_uftbHit`, `s1_uftbHasIndirect`, `s1_ftbCloseReq` 플래그
- `is_fast_pred = true` — 1 사이클 조회

### 5.3 FTB + TAGE base 예측 (S2)

- Main FTB: SRAM 기반, tag 비교, FTBEntry 반환
- FTBEntry: `brSlots(numBrSlot)` + `tailSlot`, `pftAddr`, `carry`, `isJalr/isCall/isRet`
- `s2_redirect`: FauFTB가 예측한 target과 FTB target이 다를 때

### 5.4 TAGE+SC+ITTAGE+RAS (S3)

- **TAGE**: 다수의 테이블(다양한 history 길이), 각 테이블의 tag/ctr/u 관리
- **SC**: Statistical Corrector — TAGE 예측을 통계 보정
- **ITTAGE**: 간접 분기(jalr) target 예측 → `jalr_target` 설정
- **RAS**: Return Address Stack — call/ret 쌍 관리 (`TOSW`/`TOSR`/`NOS`/`ssp`/`sctr`)
- `s3_redirect`: S2 target과 S3 target이 다를 때

### 5.5 selectedResp (FTQ 입장)

```scala
// FrontendBundle.scala: BranchPredictionResp.selectedResp
PriorityMux(Seq(
  (s3.valid(3) && s3.hasRedirect(3)) -> s3,
  (s2.valid(3) && s2.hasRedirect(3)) -> s2,
  s1.valid(3)                        -> s1
))
```

S3 redirect > S2 redirect > S1 순으로 최종 예측 결정

---

## 6. Flow / Backpressure Control

| 조건 | 동작 |
| ---- | ---- |
| FTQ full (`ftqFullStall`) | S3 `s3_ready=false` → s2/s1 stall 연쇄 |
| S3 → S2 override | `overrideBubble(1)=true` → S2 flush, S1 flush |
| S2 → S1 override | `overrideBubble(0)=true` → S1 flush |
| redirect valid | `io.in.ready := !io.redirect.valid` → BasePredictor 입력 차단 |
| S0 stall | `s0_stall = !s3_ready` → PC 레지스터 동결 (`RegEnable(s0_pc, !s0_stall)`) |

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
| S0 PC mux | `npcGen`: redirect/s3/s2/s1/seq 5단계 우선순위 MUX — 타이밍 크리티컬 |
| S1 FauFTB lookup | fully-associative tag 비교 (uFTB entries 개수만큼 병렬 비교) |
| S2 FTB SRAM | SRAM read latency + tag 비교 |
| S3 TAGE | 다수 테이블 동시 lookup (각 다른 history 길이로 index) |
| folded history update | `bitsets_xor` 함수: compLen 비트 전체에 대한 XOR 트리 |
| `SegmentedAddrNext` | PC 상위 비트 분할 저장 (pcSegments = Seq(VAddrBits-24, 12, 12)) — 타이밍 최적화 |

---

## 9. Pseudocode

```
S0 (combinational):
  s0_pc = npcGen.output  // priority: redirect > s3 > s2 > s1 > seq
  s0_folded_gh = foldedGhGen.output
  predictors.in.valid = !s0_stall && !redirect

S1:
  if s0_fire:
    s1_pc = RegEnable(s0_pc)
    s1_valid = true
  if s1_fire:
    FauFTB.lookup(s1_pc) → s1_pred
    bpu_to_ftq.resp = {s1: s1_pred, valid}
    update npcGen (s1 priority)
  if s1_flush:
    s1_valid = false

S2:
  if s1_fire:
    s2_pc = RegEnable(s1_pc)
    s2_valid = true
  if s2_fire:
    FTB.lookup(s2_pc) + TAGE_base → s2_pred
    if s2_pred.target ≠ s1_pred.target:
      s2_redirect = true
      bpu_to_ftq.resp = {s2: s2_pred, hasRedirect=true}
      flush S1 (overrideBubble(0))
    else:
      bpu_to_ftq.resp = {s2: s2_pred}

S3:
  if s2_fire:
    s3_pc = RegEnable(s2_pc)
    s3_valid = true
  if s3_fire:
    TAGE+SC+ITTAGE+RAS → s3_pred
    if s3_pred.target ≠ s2_pred.target:
      s3_redirect = true
      bpu_to_ftq.resp = {s3: s3_pred, hasRedirect=true}
      flush S1/S2 (overrideBubble(1))
    else:
      bpu_to_ftq.resp = {s3: s3_pred}
    if FTQ full:
      ftqFullStall = true  // s3_ready = false

on redirect:
  GHR rollback to spec_info.histPtr
  RAS rollback to spec_info (ssp/sctr/TOSW/TOSR/NOS)
  npcGen = redirect.target (최고 우선순위)
```

---

## 10. Notes / Assumptions

- `numDup=4`: 동일한 PC/history를 4벌 복제 → 각각 독립적인 SRAM bank에 배치, fan-out 감소
- `SegmentedAddrNext`: PC를 분할 저장하여 SRAM read address 생성 시 타이밍 절감
- FauFTB의 `is_fast_pred=true` 설정으로 S1에서 유효한 예측 가능 (uFTB hit 시 S2/S3 override 없음)
- `Composer.scala`: 여러 BasePredictor를 체인으로 연결 — 앞 단 출력이 뒷 단 `resp_in(0)`으로 입력
- `useBPD=true` 조건에서만 Composer 사용 (FakePredictor는 bypass 목적)
