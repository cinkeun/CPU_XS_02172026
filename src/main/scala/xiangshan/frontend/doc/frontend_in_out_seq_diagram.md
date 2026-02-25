# frontend_in_out_seq_diagram.md

- Block: Frontend
- Module: N/A (전체 I/O 흐름)
- Source: Frontend.scala, ifu/Ifu.scala, ftq/Ftq.scala, bpu/Bpu.scala, ibuffer/IBuffer.scala
- Protocols: Decoupled / Valid
- Key Params: FtqSize=64, FetchBlockInstNum=16, DecodeWidth=6, IBuffer.Size=48, NumWriteBank=4, NumReadBank=8
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)

---

## 1. I/O Summary

### 1.1 입력 (FrontendInlinedImp.io)

| Port | Protocol | 방향 | 설명 |
| ---- | -------- | ---- | ---- |
| `io.backend.toFtq.redirect` | Valid | Backend → FTQ | Misprediction/MemVio redirect |
| `io.backend.toFtq.commit` | Valid FtqPtr | Backend → FTQ | ROB commit 신호 (FTQ commit ptr 갱신, MMIO lastCommit 체크용) |
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
  BPU->>BPU: [C1] S1: UBTB+ABTB+UTAGE+MicroRAS lookup → 1st prediction
  BPU->>FTQ: [C1] io.toFtq.prediction.valid (s1 pred, startPc, ftqIdx)
  FTQ-->>BPU: io.toFtq.prediction.ready

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

## 3. Sequence Diagram — Group B: BPU S3 Override (s3_override → FTQ entry 갱신 + IFU flush)

> **실제 아키텍처 (코드 근거: `bpu/Bpu.scala`, `ftq/Ftq.scala`):**
>
> BPU → FTQ 경로는 **두 가지 독립적인 흐름**이 있다:
>
> | 경로 | 조건 | FTQ 동작 | latency |
> |------|------|-----------|---------|
> | **S1 신규 enq** | `s1_valid && s2_ready` | `entryQueue[bpuPtr]` 신규 기록, `bpuPtr++` | ~1 cycle |
> | **S3 override** | `s3_override` (S3≠S1) | `entryQueue[s3FtqPtr]` 덮어쓰기, `bpuPtr := s3FtqPtr+1` | ~3 cycle |
>
> - **S1 (uBTB/ABTB) 예측은 항상 FTQ에 먼저 들어간다.** BPU latency가 3-cycle 고정이 아니다.
> - **S3 override**는 S3 결과가 S1 결과와 다를 때(`s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)`)만 발생하며, 기존에 S1이 enq한 entry를 **사후 교정**한다.
> - S2는 BPU 내부 flush(`s2_flush := s3_flush || s3_override`)만 처리하며 FTQ에 직접 전달하지 않는다.
>   (참고: Ftq.scala TODO — "wait for Ifu/ICache to remove bpu s2 flush")
> - **prediction.ready 비의존**: `bpuS3Redirect = prediction.valid && s3Override` — FTQ full이어도 override는 실행된다.

### FTQ 인덱스 추적 (s3FtqPtr)

| 단계 | 코드 근거 | 설명 |
|------|-----------|------|
| S1 fire 시 | `s2_ftqPtr = RegEnable(io.fromFtq.bpuPtr, s1_fire)` | S1 fire 시점의 bpuPtr를 s2에 래치 |
| S2 fire 시 | `s3_ftqPtr = RegEnable(s2_ftqPtr, s2_fire)` | s2_ftqPtr를 s3에 전달 |
| S3 시점 | `io.toFtq.s3FtqPtr := s3_ftqPtr` | FTQ에 override 대상 entry 인덱스 전달 |
| override 실행 | `predictionPtr = s3Override ? s3FtqPtr : bpuPtr(0)` | entryQueue(s3FtqPtr) 갱신 |
| bpuPtr 롤백 | `when(s3Override) { bpuPtr := s3FtqPtr + 1 }` | FTQ enqueue 포인터를 override entry 다음으로 이동 |

### FTQ Override 동작 요약

| 조건 | 동작 | 코드 근거 |
|------|------|-----------|
| prediction.fire (s1, not s3Override) | entryQueue(bpuPtr) 신규 기록, bpuPtr+1 | `prediction.fire` |
| s3Override=1 | entryQueue(s3FtqPtr) 갱신(덮어쓰기), bpuPtr := s3FtqPtr+1 | `bpuS3Redirect`, `predictionPtr` |
| ifuPtr >= s3FtqPtr | ifuPtr := s3FtqPtr (롤백) | `when(ifuPtr >= ftqIdx)` |
| pfPtr >= s3FtqPtr | pfPtr := s3FtqPtr (롤백) | `when(pfPtr >= ftqIdx)` |
| IFU stage idx >= s3FtqPtr | shouldFlushByStage3 = true → flush | `!isAfter(s3FtqPtr, idxToFlush)` |

### nextStartVAddr Bypass 경우

| bpuPtr 위치 | nextStartVAddr 소스 | 코드 근거 |
|-------------|---------------------|-----------|
| bpuPtr(0) == ifuPtr(0) | prediction.bits.target (bypass-1) | `bpuPtr(0) === ifuPtr(0)` |
| bpuPtr(0) == ifuPtr(1) | prediction.bits.startPc (bypass-2, = tgt_C) | `bpuPtr(0) === ifuPtr(1)` |
| 그 외 | entryQueue(ifuPtr(1)).startPc (SRAM) | default MuxCase |

---

### B-1: Standard S3 Override (ifuPtr > s3FtqPtr — IFU가 이미 앞선 상태)

> S1에서 idx=X enq → IFU가 idx=X 이상 처리 중 → S3에서 s3_override=1 (tgt_C ≠ tgt_A)
> → entryQueue(X) 갱신 + ifuPtr 롤백 + IFU flush + tgt_C 기준 재시작

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as Ifu

  Note over BPU,FTQ: Cycle 0 — S1 fire: PC_A → FTQ idx=X enq
  BPU->>FTQ: [C0] prediction.valid=1, s3Override=0, startPc=PC_A, tgt=tgt_A
  FTQ-->>BPU: [C0] prediction.ready=1
  FTQ->>FTQ: [C0] entryQueue(X).startPc:=PC_A, bpuPtr:=X+1
  FTQ->>IFU: [C0] toIfu.req (idx=X, startAddr=PC_A)

  Note over BPU,IFU: Cycle 1 — S1 PC_A+blk → idx=X+1 enq, IFU idx=X 처리 중
  BPU->>FTQ: [C1] prediction.valid=1, s3Override=0, startPc=PC_A+blk, tgt=tgt_A2
  FTQ->>FTQ: [C1] entryQueue(X+1).startPc:=PC_A+blk, bpuPtr:=X+2
  FTQ->>IFU: [C1] toIfu.req (idx=X+1, startAddr=PC_A+blk)
  Note over IFU: ifuPtr=X+1, bpuPtr=X+2

  Note over BPU,IFU: Cycle 2 — S3 fire: s3_override=1, s3FtqPtr=X
  BPU->>BPU: [C2] S3: s3_prediction(PC_A)=tgt_C ≠ s1_prediction(PC_A)=tgt_A → s3_override=1
  BPU->>BPU: [C2] s2_flush:=1 (BPU 내부 S1+S2 in-flight kill), s0_startPc:=tgt_C
  BPU->>FTQ: [C2] prediction.valid=1, s3Override=1, s3FtqPtr=X, startPc=PC_A, tgt=tgt_C
  FTQ->>FTQ: [C2] bpuS3Redirect=1 → entryQueue(X) 갱신: tgt:=tgt_C
  FTQ->>FTQ: [C2] bpuPtr := X+1 (entry X+1 폐기)
  FTQ->>FTQ: [C2] ifuPtr=X+1 >= s3FtqPtr=X → ifuPtr := X (롤백)
  FTQ->>FTQ: [C2] pfPtr >= X → pfPtr := X (롤백)
  FTQ->>IFU: [C2] flushFromBpu.s3.valid=1, bits=X
  IFU->>IFU: [C2] shouldFlushByStage3(idx>=X) → s0/s1_flush=1, 모든 in-flight 무효화

  Note over FTQ,IFU: Cycle 3 — IFU 재요청 (bypass-2 적용)
  Note over FTQ: bpuPtr=X+1, ifuPtr=X → bpuPtr(0)==ifuPtr(1): bypass-2
  FTQ->>IFU: [C3] toIfu.req (idx=X, startAddr=PC_A)
  Note over FTQ: nextStartVAddr = prediction.bits.startPc = tgt_C (bypass-2, SRAM 미사용)
  IFU-->>FTQ: [C3] toIfu.req.ready
```

---

### B-2: Early S3 Override (ifuPtr <= s3FtqPtr — IFU가 아직 뒤처진 상태)

> FTQ back-pressure 등으로 IFU stall → ifuPtr < s3FtqPtr
> → ifuPtr 롤백 불필요, entryQueue(s3FtqPtr)만 갱신, IFU flush 범위 없음

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as Ifu

  Note over BPU,IFU: Cycle 0 — S1 fire: PC_A → idx=X enq, IFU stall (ifuPtr=X-1)
  BPU->>FTQ: [C0] prediction.valid=1, s3Override=0, startPc=PC_A, tgt=tgt_A
  FTQ-->>BPU: [C0] prediction.ready=1
  FTQ->>FTQ: [C0] entryQueue(X).startPc:=PC_A, bpuPtr:=X+1
  Note over IFU: IFU stall: ifuPtr=X-1 (FTQ→IFU 요청 pending 또는 미전송)

  Note over BPU: Cycle 1 — S2 fire: BPU 내부 처리
  BPU->>BPU: [C1] S2: prediction 계산 중, s3_override 준비

  Note over BPU,IFU: Cycle 2 — S3 fire: s3_override=1, ifuPtr < s3FtqPtr=X
  BPU->>BPU: [C2] S3: s3_prediction(PC_A)=tgt_C ≠ s1_prediction=tgt_A → s3_override=1
  BPU->>FTQ: [C2] prediction.valid=1, s3Override=1, s3FtqPtr=X, tgt=tgt_C
  FTQ->>FTQ: [C2] bpuS3Redirect=1 → entryQueue(X): tgt:=tgt_C
  FTQ->>FTQ: [C2] bpuPtr := X+1
  Note over FTQ: ifuPtr=X-1 < s3FtqPtr=X → ifuPtr 롤백 없음
  FTQ->>IFU: [C2] flushFromBpu.s3.valid=1, bits=X
  IFU->>IFU: [C2] shouldFlushByStage3(idx=X-1): !isAfter(X, X-1)=false → flush 없음
  Note over IFU: in-flight fetch idx=X-1 보존 (s3FtqPtr=X보다 이전 entry)

  Note over FTQ,IFU: Cycle 3 — IFU, 갱신된 tgt_C로 idx=X 정상 요청
  FTQ->>IFU: [C3] toIfu.req (idx=X, startAddr=PC_A)
  Note over FTQ: nextStartVAddr = entryQueue(X).startPc (SRAM, 이미 tgt_C로 갱신됨)
  IFU-->>FTQ: [C3] toIfu.req.ready
```

---

### B-3: Back-pressure S3 Override (prediction.ready=0 — override는 독립 실행)

> FTQ full → prediction.ready=0 (신규 S1 enq 불가)
> 그러나 s3_override는 `bpuS3Redirect` 경로로 prediction.ready와 무관하게 실행

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as Ifu

  Note over BPU,FTQ: Cycle 0 — FTQ full: prediction.ready=0
  BPU->>FTQ: [C0] prediction.valid=1, s3Override=0 (신규 S1 시도)
  FTQ-->>BPU: [C0] prediction.ready=0 (FTQ full: bpuPtr - deqPtr >= FtqSize)
  Note over BPU: s1_fire=0 (stall) — 단, 이미 S3에 래치된 PC_A에 대한 s3_override 준비됨

  Note over BPU,IFU: Cycle 1 — S3 override 발생 (prediction.ready=0 상태에서도 실행)
  BPU->>BPU: [C1] S3: s3_prediction(PC_A)=tgt_C ≠ s1_prediction=tgt_A → s3_override=1
  BPU->>FTQ: [C1] prediction.valid=1, s3Override=1, s3FtqPtr=X, tgt=tgt_C
  Note over FTQ: bpuS3Redirect = prediction.valid && s3Override = 1 (ready 비의존)
  FTQ->>FTQ: [C1] bpuS3Redirect=1 → entryQueue(X): tgt:=tgt_C (prediction.fire 무관)
  FTQ->>FTQ: [C1] bpuPtr := X+1
  FTQ->>FTQ: [C1] ifuPtr >= X → ifuPtr := X (롤백)
  FTQ->>IFU: [C1] flushFromBpu.s3.valid=1, bits=X → IFU flush

  Note over BPU,FTQ: Cycle 2 — FTQ deq 후 prediction.ready=1 복구
  FTQ-->>BPU: [C2] prediction.ready=1 (FTQ slot 확보)
  BPU->>FTQ: [C2] prediction.valid=1, s3Override=0, startPc=tgt_C (신규 S1)
  FTQ->>IFU: [C2] toIfu.req (idx=X, startAddr=PC_A, nextStartVAddr=tgt_C)
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
  BPU->>FTQ: [CN+2] io.toFtq.prediction (새 예측)

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
  BE->>FTQ: toFtq.commit.valid (CtrlToFtqIO.commit — ROB commit ptr)
  FTQ-->>IFU: mmioCommitRead.mmioLastCommit (commit ptr >= mmioPtr 여부)

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

- `allowEnq := io.in.bits.prevInstrCount < nextNumInvalid` (nextNumInvalid = Size.U − nextNumValid) — 다음 사이클의 invalid 엔트리 수보다 다음 fetch 명령어 수가 적을 때만 enqueue 허용
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
