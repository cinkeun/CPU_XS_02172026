# frontend_in_out_seq_diagram.md

- Block: Frontend
- Module: N/A (total I/O flow)
- Source: Frontend.scala, ifu/Ifu.scala, ftq/Ftq.scala, bpu/Bpu.scala, ibuffer/IBuffer.scala
- Protocols: Decoupled / Valid
- Key Params: FtqSize=64, FetchBlockInstNum=16, DecodeWidth=6, IBuffer.Size=48, NumWriteBank=4, NumReadBank=8
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)

---

## 1. I/O Summary

### 1.1 input (FrontendInlinedImp.io)

| Port | Protocol | direction | Description |
| ---- | -------- | ---- | ---- |
| `io.backend.toFtq.redirect` | Valid | Backend → FTQ | Misprediction/MemVio redirect |
| `io.backend.toFtq.commit` | Valid FtqPtr | Backend → FTQ | ROB commit signal (for FTQ commit ptr update, MMIO lastCommit check) |
| `io.backend.canAccept` | Bool | Backend → IBuffer | Decode is acceptable |
| `io.backend.wfi.wfiReq` | Bool | Backend → ICache/Uncache | WFI request |
| `io.reset_vector` | UInt | SoC → BPU | Start PC on reset |
| `io.sfence` | Bundle | CSR → iTLB | TLB flush |
| `io.tlbCsr` | Bundle | CSR → iTLB/BPU | SATP/STATUS etc. |
| `io.csrCtrl` | Bundle | CSR → Each module | bp_ctrl, frontend_trigger, pf_ctrl, etc. |
| `io.ptw` | TlbPtwIO | PTW → iTLB | Page table walk response |
| `io.softPrefetch` | Valid Vec | Backend → ICache | Software prefetch request |
| `io.debugTopDown.robHeadVaddr` | Valid | Backend → iTLB | For TopDown analysis |

### 1.2 output

| Port | Protocol | direction | Description |
| ---- | -------- | ---- | ---- |
| `io.backend.cfVec` | DecoupledIO Vec | Frontend → Decode | DecodeWidth CtrlFlow command |
| `io.backend.fromFtq` | Bundle | FTQ → Backend | PC mem write, newest entry, etc. |
| `io.backend.fromIfu` | Bundle | IFU → Backend | gpaddr mem write |
| `io.backend.wfi.wfiSafe` | Bool | Frontend → Backend | Is it safe to enter WFI |
| `io.error` | L1BusErrorUnitInfo | ICache → Backend | ECC/Bus Error |
| `io.frontendInfo.ibufFull` | Bool | IBuffer → Backend | IBuffer full status |
| `io.frontendInfo.bpuInfo` | Bundle | FTQ → Backend | BPU hit/miss counter |
| `io.resetInFrontend` | Bool | Frontend → SoC | reset signal propagation |

---

## 2. Sequence Diagram — Group A: Normal Fetch Flow (ICache Hit)

> BPU prediction → FTQ enqueue → IFU fetch → ICache hit → IBuffer enqueue → Decode delivery

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
IFU->>IFU: [C2] F1: PC calculation (CatPC adder), f1_valid=1

  Note over IFU,IC: Cycle 3 — IFU F2, ICache hit resp
  IC->>IFU: [C3] icache.fetch.resp.valid (data, exception, paddr)
IFU->>IFU: [C3] F2: predecode, exception generation, instr range, f2_valid=1

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

## 3. Sequence Diagram — Group B: BPU S3 Override (s3_override → FTQ entry update + IFU flush)

> **Actual architecture (code basis: `bpu/Bpu.scala`, `ftq/Ftq.scala`):**
>
> The BPU → FTQ path has **two independent flows**:
>
> | path | Conditions | FTQ operation | latency |
> |------|------|-----------|---------|
> | **S1 new enq** | `s1_valid && s2_ready` | `entryQueue[bpuPtr]` new record, `bpuPtr++` | ~1 cycle |
> | **S3 override** | `s3_override` (S3≠S1) | Overwrite `entryQueue[s3FtqPtr]`, `bpuPtr := s3FtqPtr+1` | ~3 cycle |
>
> - **S1 (uBTB/ABTB) prediction always goes into FTQ first.** BPU latency is not fixed to 3-cycle.
> - **S3 override** only occurs when the S3 result is different from the S1 result (`s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)`), and **post-corrects** the entry previously enqled by S1.
> - S2 only processes the BPU internal flush (`s2_flush := s3_flush || s3_override`) and does not directly transmit it to the FTQ.
> (Reference: Ftq.scala TODO — "wait for Ifu/ICache to remove bpu s2 flush")
> - **prediction.ready independent**: `bpuS3Redirect = prediction.valid && s3Override` — Override is executed even if FTQ is full.

### FTQ index tracking (s3FtqPtr)

| steps | Code Basis | Description |
|------|-----------|------|
| S1 fire city | `s2_ftqPtr = RegEnable(io.fromFtq.bpuPtr, s1_fire)` | Latch bpuPtr at S1 fire point to s2 |
| S2 fire city | `s3_ftqPtr = RegEnable(s2_ftqPtr, s2_fire)` | pass s2_ftqPtr to s3 |
| S3 viewpoint | `io.toFtq.s3FtqPtr := s3_ftqPtr` | Pass override target entry index to FTQ |
| run override | `predictionPtr = s3Override ? s3FtqPtr : bpuPtr(0)` | update entryQueue(s3FtqPtr) |
| rollback bpuPtr | `when(s3Override) { bpuPtr := s3FtqPtr + 1 }` | Move the FTQ enqueue pointer to the next override entry |

### FTQ Override Operation Summary

| Conditions | Action | Code Basis |
|------|------|-----------|
| prediction.fire(s1, not s3Override) | entryQueue(bpuPtr) new record, bpuPtr+1 | `prediction.fire` |
| s3Override=1 | entryQueue(s3FtqPtr) update(overwrite), bpuPtr := s3FtqPtr+1 | `bpuS3Redirect`, `predictionPtr` |
| ifuPtr >= s3FtqPtr | ifuPtr := s3FtqPtr (rollback) | `when(ifuPtr >= ftqIdx)` |
| pfPtr >= s3FtqPtr | pfPtr := s3FtqPtr (rollback) | `when(pfPtr >= ftqIdx)` |
| IFU stage idx >= s3FtqPtr | shouldFlushByStage3 = true → flush | `!isAfter(s3FtqPtr, idxToFlush)` |

### nextStartVAddr Bypass case

| bpuPtr location | nextStartVAddr source | Code Basis |
|-------------|---------------------|-----------|
| bpuPtr(0) == ifuPtr(0) | prediction.bits.target (bypass-1) | `bpuPtr(0) === ifuPtr(0)` |
| bpuPtr(0) == ifuPtr(1) | prediction.bits.startPc (bypass-2, = tgt_C) | `bpuPtr(0) === ifuPtr(1)` |
| Other | entryQueue(ifuPtr(1)).startPc (SRAM) | default MuxCase |

---

### B-1: Standard S3 Override (ifuPtr > s3FtqPtr — IFU is already ahead)

> enq idx=X in S1 → IFU is processing more than idx=X → s3_override=1 in S3 (tgt_C ≠ tgt_A)
> → update entryQueue(X) + rollback ifuPtr + IFU flush + restart based on tgt_C

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

Note over BPU,IFU: Cycle 1 — S1 PC_A+blk → idx=X+1 enq, IFU idx=X processing
  BPU->>FTQ: [C1] prediction.valid=1, s3Override=0, startPc=PC_A+blk, tgt=tgt_A2
  FTQ->>FTQ: [C1] entryQueue(X+1).startPc:=PC_A+blk, bpuPtr:=X+2
  FTQ->>IFU: [C1] toIfu.req (idx=X+1, startAddr=PC_A+blk)
  Note over IFU: ifuPtr=X+1, bpuPtr=X+2

  Note over BPU,IFU: Cycle 2 — S3 fire: s3_override=1, s3FtqPtr=X
  BPU->>BPU: [C2] S3: s3_prediction(PC_A)=tgt_C ≠ s1_prediction(PC_A)=tgt_A → s3_override=1
BPU->>BPU: [C2] s2_flush:=1 (BPU internal S1+S2 in-flight kill), s0_startPc:=tgt_C
  BPU->>FTQ: [C2] prediction.valid=1, s3Override=1, s3FtqPtr=X, startPc=PC_A, tgt=tgt_C
FTQ->>FTQ: [C2] bpuS3Redirect=1 → update entryQueue(X): tgt:=tgt_C
FTQ->>FTQ: [C2] bpuPtr := X+1 (discard entry X+1)
FTQ->>FTQ: [C2] ifuPtr=X+1 >= s3FtqPtr=X → ifuPtr := X (rollback)
FTQ->>FTQ: [C2] pfPtr >= X → pfPtr := X (rollback)
  FTQ->>IFU: [C2] flushFromBpu.s3.valid=1, bits=X
IFU->>IFU: [C2] shouldFlushByStage3(idx>=X) → s0/s1_flush=1, invalidate all in-flight

Note over FTQ,IFU: Cycle 3 — IFU re-request (bypass-2 applied)
  Note over FTQ: bpuPtr=X+1, ifuPtr=X → bpuPtr(0)==ifuPtr(1): bypass-2
  FTQ->>IFU: [C3] toIfu.req (idx=X, startAddr=PC_A)
Note over FTQ: nextStartVAddr = prediction.bits.startPc = tgt_C (bypass-2, SRAM not used)
  IFU-->>FTQ: [C3] toIfu.req.ready
```

---

### B-2: Early S3 Override (ifuPtr <= s3FtqPtr — IFU is still behind)

> IFU stall due to FTQ back-pressure, etc. → ifuPtr < s3FtqPtr
> → no need for ifuPtr rollback, only update entryQueue(s3FtqPtr), no IFU flush range

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as Ifu

  Note over BPU,IFU: Cycle 0 — S1 fire: PC_A → idx=X enq, IFU stall (ifuPtr=X-1)
  BPU->>FTQ: [C0] prediction.valid=1, s3Override=0, startPc=PC_A, tgt=tgt_A
  FTQ-->>BPU: [C0] prediction.ready=1
  FTQ->>FTQ: [C0] entryQueue(X).startPc:=PC_A, bpuPtr:=X+1
Note over IFU: IFU stall: ifuPtr=X-1 (FTQ→IFU request pending or not sent)

Note over BPU: Cycle 1 — S2 fire: BPU internal processing
BPU->>BPU: [C1] S2: Calculating prediction, preparing s3_override

  Note over BPU,IFU: Cycle 2 — S3 fire: s3_override=1, ifuPtr < s3FtqPtr=X
  BPU->>BPU: [C2] S3: s3_prediction(PC_A)=tgt_C ≠ s1_prediction=tgt_A → s3_override=1
  BPU->>FTQ: [C2] prediction.valid=1, s3Override=1, s3FtqPtr=X, tgt=tgt_C
  FTQ->>FTQ: [C2] bpuS3Redirect=1 → entryQueue(X): tgt:=tgt_C
  FTQ->>FTQ: [C2] bpuPtr := X+1
Note over FTQ: ifuPtr=X-1 < s3FtqPtr=X → no ifuPtr rollback
  FTQ->>IFU: [C2] flushFromBpu.s3.valid=1, bits=X
IFU->>IFU: [C2] shouldFlushByStage3(idx=X-1): !isAfter(X, X-1)=false → no flush
Note over IFU: Preserve in-flight fetch idx=X-1 (entry before s3FtqPtr=X)

Note over FTQ,IFU: Cycle 3 — IFU, idx=X normal request with updated tgt_C
  FTQ->>IFU: [C3] toIfu.req (idx=X, startAddr=PC_A)
Note over FTQ: nextStartVAddr = entryQueue(X).startPc (SRAM, already updated with tgt_C)
  IFU-->>FTQ: [C3] toIfu.req.ready
```

---

### B-3: Back-pressure S3 Override (prediction.ready=0 — override runs independently)

> FTQ full → prediction.ready=0 (new S1 enq not possible)
> However, s3_override is executed regardless of prediction.ready with `bpuS3Redirect` path.

```mermaid
sequenceDiagram
  participant BPU as Predictor (BPU)
  participant FTQ as Ftq
  participant IFU as Ifu

  Note over BPU,FTQ: Cycle 0 — FTQ full: prediction.ready=0
BPU->>FTQ: [C0] prediction.valid=1, s3Override=0 (try new S1)
  FTQ-->>BPU: [C0] prediction.ready=0 (FTQ full: bpuPtr - deqPtr >= FtqSize)
Note over BPU: s1_fire=0 (stall) — However, s3_override is ready for PC_A already latched to S3

Note over BPU,IFU: Cycle 1 — S3 override occurs (executes even when prediction.ready=0)
  BPU->>BPU: [C1] S3: s3_prediction(PC_A)=tgt_C ≠ s1_prediction=tgt_A → s3_override=1
  BPU->>FTQ: [C1] prediction.valid=1, s3Override=1, s3FtqPtr=X, tgt=tgt_C
Note over FTQ: bpuS3Redirect = prediction.valid && s3Override = 1 (ready independent)
FTQ->>FTQ: [C1] bpuS3Redirect=1 → entryQueue(X): tgt:=tgt_C (prediction.fire is irrelevant)
  FTQ->>FTQ: [C1] bpuPtr := X+1
FTQ->>FTQ: [C1] ifuPtr >= X → ifuPtr := X (rollback)
  FTQ->>IFU: [C1] flushFromBpu.s3.valid=1, bits=X → IFU flush

Note over BPU,FTQ: Cycle 2 — Recover prediction.ready=1 after FTQ deq
FTQ-->>BPU: [C2] prediction.ready=1 (Secure FTQ slot)
BPU->>FTQ: [C2] prediction.valid=1, s3Override=0, startPc=tgt_C (New S1)
  FTQ->>IFU: [C2] toIfu.req (idx=X, startAddr=PC_A, nextStartVAddr=tgt_C)
```

---

## 4. Sequence Diagram — Group C: Backend Redirect (Misprediction Flush)

> Detect misprediction in ROB → Flush entire Frontend → Restart with correct PC

```mermaid
sequenceDiagram
  participant BE  as Backend (ROB)
  participant FTQ as Ftq
  participant BPU as Predictor
  participant IFU as NewIFU
  participant IBuf as IBuffer

  Note over BE,FTQ: Cycle N — Backend redirect
  BE->>FTQ: [CN] toFtq.redirect.valid (BranchPredictionRedirect: target, ftqIdx, ftqOffset)
BE->>BPU: [CN] (via FTQ) redirect.valid → BPU in-flight invalidation

  Note over FTQ,IBuf: Cycle N+1 — needFlush=RegNext(redirect.valid)
  FTQ->>IFU: [CN+1] toIfu.redirect.valid → f0/f1/f2/f3_flush
FTQ->>IFU: [CN+1] icacheFlush → Invalidate ICache pipe
FTQ-->>BPU: [CN+1] toBpu.redirect.valid (BPU update + history restore)
IBuf->>IBuf: [CN+1] ibuffer.io.flush → entire IBuffer flush

Note over BPU: Cycle N+2 — BPU restart
BPU->>BPU: [CN+2] S0: Set redirect target to new PC
BPU->>FTQ: [CN+2] io.toFtq.prediction (new prediction)

Note over FTQ,IFU: Cycle N+3 — IFU restart
FTQ->>IFU: [CN+3] toIfu.req.valid (new FetchRequestBundle)

Note over BE: After IBuf flush, wait for new command to be supplied from IBuffer
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

Note over IC,IFU: Cycle X — L2 response, ICache miss resolution
  MISS->>IC: [CX] refill cacheline
  IC->>IFU: [CX] fetch.resp.valid=1 (f2_icache_all_resp_wire=1)
  IFU->>IFU: [CX] icacheRespAllValid=1, f2_fire=1
IFU-->>FTQ: resume fromFtq.req.ready=1

Note over IFU: TopDown: icacheMissBubble → record topdown_stages(1)
```

---

## 6. Sequence Diagram — Group E: MMIO Fetch

> MMIO area command fetch → Via InstrUncache → Next MMIO fetch after ROB commit

```mermaid
sequenceDiagram
  participant IFU as NewIFU (F3)
  participant FTQ as Ftq
  participant UC  as InstrUncache
  participant BE  as Backend (ROB)

Note over IFU: F2: detect pmp_mmio
IFU->>IFU: [C] f2_pmp_mmio=1 → Enter MMIO mode at F3

Note over IFU,UC: F3: MMIO fetch request
  IFU->>UC: [C] toUncache.valid (InsUncacheReq: addr)
  UC-->>IFU: toUncache.ready
  UC->>IFU: [C+L] fromUncache.valid (InsUncacheResp: data)
  IFU-->>UC: fromUncache.ready

Note over IFU,FTQ: MMIO instr → IBuffer (1 each)
IFU->>FTQ: mmioCommitRead.valid=1 (MMIO FtqPtr lookup)
  BE->>FTQ: toFtq.commit.valid (CtrlToFtqIO.commit — ROB commit ptr)
FTQ-->>IFU: mmioCommitRead.mmioLastCommit (whether commit ptr >= mmioPtr)

  alt mmioLastCommit=1
IFU->>IFU: [C] mmio_redirect → flush to next MMIO PC
    IFU->>FTQ: [C] pdWb (misOffset, target)
  else
IFU->>IFU: [C] standby (stall)
  end
```

---

## 7. Edge Cases

### 7.1 IBuffer full → fetch stall

- `allowEnq := io.in.bits.prevInstrCount < nextNumInvalid` (nextNumInvalid = Size.U − nextNumValid) — Enqueue is allowed only when the number of next fetch instructions is less than the number of invalid entries in the next cycle.
- `io.full = !allowEnq` → `io.frontendInfo.ibufFull` output → stall signal to backend
- IFU `toIbuffer.ready` = false → F3 stall → F2 stall → FTQ back-pressure

### 7.2 FTQ full → BPU stall

- `ftqFullStall` → BPU S3 `s3_ready=false` → s2/s1 also chain stall
- `s0_stall = !s3_ready` → PC mux freeze in BPU S0

### 7.3 Cross-cacheline fetch (doubleLine)

- `f0_doubleLine = fromFtq.req.bits.crossCacheline` — `startAddr(blockOffBits-1) === 1`
- Added second cacheline request to ICache (FtqToICacheRequestBundle.readValid(1..4))
- F2: `f2_doubleLine && f2_icache_all_resp_wire = vaddr(0)===startAddr && vaddr(1)===nextlineStart`

### 7.4 Cross-page RVI instruction exception

- F2: `isLastInLine(f2_pc(i)) && !f2_pd(i).isRVC && f2_doubleLine` → `f2_crossPage_exception_vec`
- If there is no exception on the first page, use the exception on the second page.
-`IBufferExceptionType.isCrossPage()` classification

### 7.5 Unnecessary flush (BTB Miss) classification

- `ControlBTBMissBubble = ControlRedirectBubble && !cfiUpdate.br_hit && !cfiUpdate.jr_hit`
- `TAGEMissBubble = ControlRedirectBubble && cfiUpdate.br_hit && !cfiUpdate.sc_hit`
- `SCMissBubble` / `ITTAGEMissBubble` / `RASMissBubble`
- IBuffer: Counter input for each bubble type (TopDown analysis)

---

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_Predictor_analysis.md](./frontend_Predictor_analysis.md)
→ See [frontend_NewIFU_analysis.md](./frontend_NewIFU_analysis.md)
→ See [frontend_Ftq_analysis.md](./frontend_Ftq_analysis.md)
→ See [frontend_IBuffer_analysis.md](./frontend_IBuffer_analysis.md)
