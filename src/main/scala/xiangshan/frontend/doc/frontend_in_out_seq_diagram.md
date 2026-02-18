# frontend_in_out_seq_diagram.md

- Block: Frontend
- Module: N/A (전체 I/O 흐름)
- Source: Frontend.scala, IFU.scala, NewFtq.scala, BPU.scala, IBuffer.scala
- Protocols: Decoupled / Valid
- Key Params: FtqSize=64, PredictWidth=16, DecodeWidth=6, IBufSize=48
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)

---

## 1. I/O Summary

### 1.1 입력 (FrontendInlinedImp.io)

| Port | Protocol | 방향 | 설명 |
| ---- | -------- | ---- | ---- |
| `io.backend.toFtq.redirect` | Valid | Backend → FTQ | Misprediction/MemVio redirect |
| `io.backend.toFtq.rob_commits` | Valid Vec | Backend → IFU | ROB commit 정보 (MMIO 제어) |
| `io.backend.canAccept` | Bool | Backend → IBuffer | Decode가 수락 가능한지 |
| `io.backend.wfi.wfiReq` | Bool | Backend → ICache/Uncache | WFI 요청 |
| `io.reset_vector` | UInt | SoC → BPU | 리셋 시 시작 PC |
| `io.sfence` | Bundle | CSR → iTLB | TLB flush |
| `io.tlbCsr` | Bundle | CSR → iTLB/BPU | SATP/STATUS 등 |
| `io.csrCtrl` | Bundle | CSR → 각 모듈 | bp_ctrl, frontend_trigger, pf_ctrl 등 |
| `io.ptw` | TlbPtwIO | PTW → iTLB | Page table walk 응답 |
| `io.softPrefetch` | Valid Vec | Backend → ICache | 소프트웨어 prefetch 요청 |
| `io.debugTopDown.robHeadVaddr` | Valid | Backend → iTLB | TopDown 분석용 |

### 1.2 출력

| Port | Protocol | 방향 | 설명 |
| ---- | -------- | ---- | ---- |
| `io.backend.cfVec` | DecoupledIO Vec | Frontend → Decode | DecodeWidth개 CtrlFlow 명령어 |
| `io.backend.fromFtq` | Bundle | FTQ → Backend | PC mem write, newest entry 등 |
| `io.backend.fromIfu` | Bundle | IFU → Backend | gpaddr mem write |
| `io.backend.wfi.wfiSafe` | Bool | Frontend → Backend | WFI 진입 안전 여부 |
| `io.error` | L1BusErrorUnitInfo | ICache → Backend | ECC/버스 에러 |
| `io.frontendInfo.ibufFull` | Bool | IBuffer → Backend | IBuffer full 상태 |
| `io.frontendInfo.bpuInfo` | Bundle | FTQ → Backend | BPU 적중/미스 카운터 |
| `io.resetInFrontend` | Bool | Frontend → SoC | 리셋 신호 전파 |

---

## 2. Sequence Diagram — Group A: 정상 Fetch 흐름 (ICache Hit)

> BPU 예측 → FTQ enqueue → IFU fetch → ICache hit → IBuffer enqueue → Decode 전달

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as NewIFU
  participant IC  as ICache
  participant IBuf as IBuffer
  participant BE  as Backend/Decode

  Note over BPU,FTQ: Cycle 0 — BPU S0
  BPU->>BPU: [C0] S0: PC mux, history gen

  Note over BPU,FTQ: Cycle 1 — BPU S1
  BPU->>BPU: [C1] S1: FauFTB lookup → 1st prediction
  BPU->>FTQ: [C1] bpu_to_ftq.resp.valid (s1 pred, ftq_idx)
  FTQ-->>BPU: bpu_to_ftq.resp.ready

  Note over FTQ,IFU: Cycle 1 — FTQ enqueue, IFU F0
  FTQ->>IFU: [C1] toIfu.req.valid (FetchRequestBundle: startAddr, nextlineStart, ftqIdx)
  FTQ->>IC:  [C1] toICache.req.valid (FtqToICacheRequestBundle)
  IFU-->>FTQ: fromFtq.req.ready (f1_ready && icacheReady)

  Note over IFU,IC: Cycle 2 — IFU F1, ICache S0
  IFU->>IFU: [C2] F1: PC 계산 (CatPC adder), f1_valid=1

  Note over IFU,IC: Cycle 3 — IFU F2, ICache hit resp
  IC->>IFU: [C3] icache.fetch.resp.valid (data, exception, paddr)
  IFU->>IFU: [C3] F2: predecode, exception 생성, instr range, f2_valid=1

  Note over IFU,IBuf: Cycle 4 — IFU F3 → IBuffer
  IFU->>IFU: [C4] F3: RVC expand, PredChecker, f3_valid=1
  IFU->>IBuf: [C4] toIbuffer.valid (FetchToIBuffer: instrs, pd, pc, ftqPtr)
  IBuf-->>IFU: toIbuffer.ready (allowEnq)
  IFU->>FTQ:  [C4] pdWb.valid (PredecodeWritebackBundle)

  Note over IBuf,BE: Cycle 5 — IBuffer → Decode
  IBuf->>BE: [C5] cfVec(0..5).valid (CtrlFlow × DecodeWidth)
  BE-->>IBuf: cfVec(i).ready (decodeCanAccept)
```

---

## 3. Sequence Diagram — Group B: BPU Redirect (S2/S3 Override)

> BPU S2 또는 S3가 S1 예측보다 더 정확한 target 계산 → FTQ에 override, IFU flush

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as NewIFU

  Note over BPU: Cycle 1 — S1 예측 (FauFTB)
  BPU->>FTQ: [C1] bpu_to_ftq (s1 pred, target_A)

  Note over BPU: Cycle 2 — S2 예측 (Main FTB)
  BPU->>BPU: [C2] s2_redirect 감지: target_B ≠ target_A
  BPU->>FTQ: [C2] bpu_to_ftq (s2 pred, target_B, hasRedirect=true)
  FTQ-->>IFU: [C2] flushFromBpu.s2.valid (FtqPtr 기준 flush)

  Note over IFU: Cycle 2 — IFU F0 flush
  IFU->>IFU: [C2] f0_flush_from_bpu = shouldFlushByStage2(f0_ftq_req.ftqIdx)
  IFU->>IFU: [C2] f1_flush → f2_flush (연쇄)

  Note over BPU,FTQ: Cycle 3 — S3 예측 (TAGE+SC+ITTAGE+RAS)
  BPU->>BPU: [C3] s3_redirect 감지: target_C ≠ target_B
  BPU->>FTQ: [C3] bpu_to_ftq (s3 pred, target_C, hasRedirect=true)
  FTQ-->>IFU: [C3] flushFromBpu.s3.valid
  IFU->>IFU: [C3] f0_flush_from_bpu → 파이프 무효화

  Note over BPU,FTQ: 이후 — 새 target_C로 정상 fetch 재개
  BPU->>FTQ: [C4] bpu_to_ftq (s1 pred, target_C 기반 다음 PC)
```

---

## 4. Sequence Diagram — Group C: Backend Redirect (Misprediction Flush)

> ROB에서 misprediction 감지 → 전체 Frontend flush → 정확한 PC로 재시작

```mermaid
sequenceDiagram
  participant BE  as Backend (ROB)
  participant FTQ as Ftq
  participant BPU as Predictor
  participant IFU as NewIFU
  participant IBuf as IBuffer

  Note over BE,FTQ: Cycle N — Backend redirect
  BE->>FTQ: [CN] toFtq.redirect.valid (BranchPredictionRedirect: target, ftqIdx, ftqOffset)
  BE->>BPU: [CN] (via FTQ) redirect.valid → BPU in-flight 무효화

  Note over FTQ,IBuf: Cycle N+1 — needFlush=RegNext(redirect.valid)
  FTQ->>IFU: [CN+1] toIfu.redirect.valid → f0/f1/f2/f3_flush
  FTQ->>IFU: [CN+1] icacheFlush → ICache 파이프 무효화
  FTQ-->>BPU: [CN+1] toBpu.redirect.valid (BPU update + 히스토리 복원)
  IBuf->>IBuf: [CN+1] ibuffer.io.flush → 전체 IBuffer flush

  Note over BPU: Cycle N+2 — BPU 재시작
  BPU->>BPU: [CN+2] S0: redirect target을 새 PC로 설정
  BPU->>FTQ: [CN+2] bpu_to_ftq (새 예측)

  Note over FTQ,IFU: Cycle N+3 — IFU 재시작
  FTQ->>IFU: [CN+3] toIfu.req.valid (새 FetchRequestBundle)

  Note over BE: IBuf flush 이후 IBuffer에서 새 명령어 공급 대기
```

---

## 5. Sequence Diagram — Group D: ICache Miss (Stall)

> ICache miss → IFU F2 stall → FTQ back-pressure

```mermaid
sequenceDiagram
  participant FTQ as Ftq
  participant IFU as NewIFU
  participant IC  as ICache
  participant MISS as ICache MissHandler

  Note over IFU,IC: Cycle 2 — F2: ICache miss
  IFU->>IFU: [C2] f2_valid=1, f2_icache_all_resp_wire=0
  IFU->>IC:  icacheStop := !f3_ready (deassert when F3 stall)
  IC->>MISS: [C2] miss req → L2 fetch

  Note over IFU: Cycle 2~X — F2 stall
  IFU->>IFU: [C2~X] f2_fire=0 (icacheRespAllValid=0)
  IFU-->>FTQ: fromFtq.req.ready=0 (f1_ready=0 → FTQ stall)

  Note over IC,IFU: Cycle X — L2 응답, ICache miss 해소
  MISS->>IC: [CX] refill cacheline
  IC->>IFU: [CX] fetch.resp.valid=1 (f2_icache_all_resp_wire=1)
  IFU->>IFU: [CX] icacheRespAllValid=1, f2_fire=1
  IFU-->>FTQ: fromFtq.req.ready=1 재개

  Note over IFU: TopDown: icacheMissBubble → topdown_stages(1) 기록
```

---

## 6. Sequence Diagram — Group E: MMIO Fetch

> MMIO 영역 명령어 fetch → InstrUncache 경유 → ROB commit 후 다음 MMIO fetch

```mermaid
sequenceDiagram
  participant IFU as NewIFU (F3)
  participant FTQ as Ftq
  participant UC  as InstrUncache
  participant BE  as Backend (ROB)

  Note over IFU: F2: pmp_mmio 감지
  IFU->>IFU: [C] f2_pmp_mmio=1 → F3에서 MMIO 모드 진입

  Note over IFU,UC: F3: MMIO fetch 요청
  IFU->>UC: [C] toUncache.valid (InsUncacheReq: addr)
  UC-->>IFU: toUncache.ready
  UC->>IFU: [C+L] fromUncache.valid (InsUncacheResp: data)
  IFU-->>UC: fromUncache.ready

  Note over IFU,FTQ: MMIO instr → IBuffer (1개씩)
  IFU->>FTQ: mmioCommitRead.valid=1 (MMIO FtqPtr 조회)
  FTQ-->>IFU: mmioLastCommit (ROB commit 여부)
  BE->>FTQ: rob_commits (commit 신호)

  alt mmioLastCommit=1
    IFU->>IFU: [C] mmio_redirect → 다음 MMIO PC로 flush
    IFU->>FTQ: [C] pdWb (misOffset, target)
  else
    IFU->>IFU: [C] 대기 (stall)
  end
```

---

## 7. Edge Cases

### 7.1 IBuffer full → fetch stall

- `allowEnq = (IBufSize - PredictWidth).U >= numValidNext` — 거의 full 시 차단
- `io.full = !allowEnq` → `io.frontendInfo.ibufFull` 출력 → Backend에 stall 신호
- IFU `toIbuffer.ready` = false → F3 stall → F2 stall → FTQ back-pressure

### 7.2 FTQ full → BPU stall

- `ftqFullStall` → BPU S3 `s3_ready=false` → s2/s1도 연쇄 stall
- `s0_stall = !s3_ready` → BPU S0에서 PC mux 동결

### 7.3 Cross-cacheline fetch (doubleLine)

- `f0_doubleLine = fromFtq.req.bits.crossCacheline` — `startAddr(blockOffBits-1) === 1`
- ICache에 두 번째 cacheline 요청 추가 (FtqToICacheRequestBundle.readValid(1..4))
- F2: `f2_doubleLine && f2_icache_all_resp_wire = vaddr(0)===startAddr && vaddr(1)===nextlineStart`

### 7.4 Cross-page RVI instruction 예외

- F2: `isLastInLine(f2_pc(i)) && !f2_pd(i).isRVC && f2_doubleLine` → `f2_crossPage_exception_vec`
- 첫 번째 page에 예외 없으면 두 번째 page의 예외를 사용
- `IBufferExceptionType.isCrossPage()` 구분

### 7.5 불필요한 flush (BTB Miss) 분류

- `ControlBTBMissBubble = ControlRedirectBubble && !cfiUpdate.br_hit && !cfiUpdate.jr_hit`
- `TAGEMissBubble = ControlRedirectBubble && cfiUpdate.br_hit && !cfiUpdate.sc_hit`
- `SCMissBubble` / `ITTAGEMissBubble` / `RASMissBubble`
- IBuffer: 각 bubble 타입별 카운터 입력 (TopDown 분석)

---

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_Predictor_analysis.md](./frontend_Predictor_analysis.md)
→ See [frontend_NewIFU_analysis.md](./frontend_NewIFU_analysis.md)
→ See [frontend_Ftq_analysis.md](./frontend_Ftq_analysis.md)
→ See [frontend_IBuffer_analysis.md](./frontend_IBuffer_analysis.md)
