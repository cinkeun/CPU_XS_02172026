# frontend_NewIFU_analysis.md

- Block: Frontend
- Module: NewIFU
- Source: IFU.scala, PreDecode.scala, FrontendBundle.scala
- Protocols: Decoupled (toIbuffer, toUncache), Valid (pdWb, iTLBInter), ValidIO (icacheInter.resp)
- Key Params: PredictWidth, HasCExtension, CommitWidth, fetchQueueSize=2
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_in_out_seq_diagram.md](./frontend_in_out_seq_diagram.md#group-a)

---

## 1. Module Summary

- **Role**: Retrieve instructions from ICache using the fetch address provided by FTQ, predecode them, and then transfer them to IBuffer. MMIO instructions are processed separately through InstrUncache.
- **Position**: `Frontend.scala` → `Module(new NewIFU)` (inside FrontendInlinedImp)
- **Pipeline stage number**: 3 register stages (F1/F2/F3) + F0 launch phase

---

## 2. Key Parameters

| Parameter | Source | Default | Impact |
| ------------- | ------------------------------- | ------- | ----------------------------------- |
| PredictWidth | `HasXSParameter.PredictWidth` | 16 | Maximum number of half-words per fetch block |
| HasCExtension | `HasXSParameter.HasCExtension` | true | Whether RVC commands are supported (16-bit processing) |
| CommitWidth | `p(XSCoreParamsKey).CommitWidth`| 6 | rob_commits port count |
| fetchQueueSize| `HasIFUConst.fetchQueueSize` | 2 | MMIO fetch internal queue size |
| blockOffBits | `HasICacheParameters` | 6 | Cache line offset bits (64B → 6bits) |
| mmioBusWidth | `HasInstrMMIOConst` | 64 | MMIO fetch bus width (bits) |
| VAddrBits | `HasXSParameter` | 39+ | Number of virtual address bits |

---

## 3. Interfaces

| Port                        | Dir | Bitwidth       | Protocol   | Description                           |
| --------------------------- | --- | -------------- | ---------- | ------------------------------------- |
| `io.ftqInter.fromFtq.req` | in | FetchRequestBundle | Decoupled | FTQ → IFU fetch request |
| `io.ftqInter.fromFtq.redirect` | in | BranchPredictionRedirect | Valid | backend redirect |
| `io.ftqInter.fromFtq.flushFromBpu` | in | BpuFlushInfo | — | BPU S2/S3 flush information |
| `io.ftqInter.toFtq.pdWb` | out | PredecodeWritebackBundle | Valid | predecode result → FTQ |
| `io.icacheInter.resp` | in | ICacheMainPipeResp | ValidIO | ICache → IFU response |
| `io.icacheInter.icacheReady`| in | Bool | — | ICache ready (F0 req.ready condition) |
| `io.icacheStop` | out | Bool | — | ICache stop request (`!f3_ready`) |
| `io.toIbuffer` | out | FetchToIBuffer | Decoupled | IFU → IBuffer command delivery |
| `io.toBackend` | out | IfuToBackendIO | — | IFU → Backend gpaddr record |
| `io.uncacheInter.toUncache` | out | InsUncacheReq | Decoupled | MMIO fetch request |
| `io.uncacheInter.fromUncache`| in | InsUncacheResp | Decoupled | MMIO fetch response |
| `io.iTLBInter` | in/out | TlbRequestIO | — | iTLB for MMIO reconversion (block possible) |
| `io.pmp` | in/out | ICachePMPBundle | — | PMP check (last port for IFU) |
| `io.mmioCommitRead` | out | mmioCommitRead | — | MMIO FTQ pointer/lastCommit check |
| `io.rob_commits` | in | Vec(CommitWidth, Valid) | — | ROB commit information (MMIO control) |
| `io.frontendTrigger` | in | FrontendTdataDistributeIO | — | Debug trigger settings |
| `io.csr_fsIsOff` | in | Bool | — | Whether to disable FS (RVC illegal decision) |

---

## 4. Internal Pipeline / State

### 4.1 Stage details

#### F0 — Launch Phase (combinational)
```
fromFtq.req.valid → f0_valid
fromFtq.req.bits  → f0_ftq_req (startAddr, nextlineStart, ftqIdx, ftqOffset)
f0_doubleLine = startAddr(blockOffBits-1) === 1 // Whether cross-cacheline or not
f0_vSetIdx = Vec(get_idx(startAddr), get_idx(nextlineStart))
f0_fire = fromFtq.req.fire = f0_valid && f1_ready && icacheReady
f0_flush_from_bpu = shouldFlushByStage2(f0_ftq_req.ftqIdx) || shouldFlushByStage3(...)
```

#### F1 — PC calculation (1 register stage)
```
f1_valid = RegInit(false.B)
f1_ftq_req = RegEnable(f0_ftq_req, f0_fire)
f1_doubleLine = RegEnable(f0_doubleLine, f0_fire)
f1_vSetIdx = RegEnable(f0_vSetIdx, f0_fire)
f1_fire = f1_valid && f2_ready

// PC adder optimization (PcCutPoint = VAddrBits/4 - 1)
f1_pc_high = f1_ftq_req.startAddr(VAddrBits-1, PcCutPoint)
f1_pc_lower_result(i) = Cat(0.U(1.W), startAddr(PcCutPoint-1, 0)) + (i*2).U // Includes overflow bit
f1_pc = CatPC(f1_pc_lower_result, f1_pc_high, f1_pc_high_plus1)

f1_cut_ptr(i) = startAddr(blockOffBits-1, 1) + i // ICache data cut pointer
```

#### F2 — ICache response processing (1 register stage)
```
f2_valid = RegInit(false.B)
f2_ftq_req = RegEnable(f1_ftq_req, f1_fire)
f2_doubleLine = RegEnable(f1_doubleLine, f1_fire)
f2_pc = CatPC(RegEnable(f1_pc_lower_result), ...)
f2_fire = f2_valid && f3_ready && icacheRespAllValid

// Check ICache response validity (timing critical)
f2_icache_all_resp_wire =
  fromICache.valid &&
  fromICache.bits.vaddr(0) === f2_ftq_req.startAddr &&
  (!f2_doubleLine || fromICache.bits.vaddr(1) === f2_ftq_req.nextlineStart)

// data processing: 5 bank × 8B = 40B duplicate the response twice and extract the intermediate response
f2_data_2_cacheline = Cat(fromICache.bits.data, fromICache.bits.data)
f2_cut_data = cut(f2_data_2_cacheline, f2_cut_ptr) // PredictWidth+1 16-bit pieces

// create exception
f2_exception = ExceptionType.merge(f2_exception_in, f2_mmio_mismatch_exception)
f2_exception_vec(i) = Exception in the cacheline to which each pc(i) belongs

// Predecode (combinational, driven by f2_valid)
preDecoder.in = {f2_cut_data, f2_pc, frontendTrigger}
f2_pd = preDecoder.out.pd      // PreDecodeInfo per instruction
f2_instr = preDecoder.out.instr
f2_jump_offset = preDecoder.out.jumpOffset

//command range
f2_jump_range = Fill(PW, !ftqOffset.valid) | Fill(PW,1)>>~ftqOffset.bits
f2_ftr_range  = Fill(PW, ftqOffset.valid) | Fill(PW,1)>>~getBasicBlockIdx(nextStartAddr,startAddr)
f2_instr_range = f2_jump_range & f2_ftr_range
```

#### F3 — IBuffer transfer (1 register stage)
```
f3_valid = RegInit(false.B)
f3_ftq_req = RegEnable(f2_ftq_req, f2_fire)
f3_fire = io.toIbuffer.fire

// RVC extension
expanders(i): RVCExpander
f3_expd_instr(i) = Mux(expander.io.ill, original, expander.io.out.bits)
f3_ill(i) = expander.io.ill // illegal RVC command

// F3Predecoder: Determine brType/isCall/isRet (complement incomplete information in F2 predecode)
f3Predecoder.in.instr = f3_instr
f3_pd(i).brType = f3Predecoder.out.pd(i).brType

// PredChecker: BPU prediction vs actual decode result comparison
checkerIn = {ftqOffset, jumpOffset, target, instrRange, instrValid, pds, pc, fire_in}
checkerOutStage1 = immediate results (jal target mismatch, etc.)
checkerOutStage2 = 1-cycle delay result (more complex check)

// wb_redirect: IFU redirects itself to FTQ
wb_redirect = checkerOutStage1.valid (when mispred is detected)

// MMIO processing
if f3_pmp_mmio:
Enter MMIO mode → toUncache.valid = true
  mmio_redirect = true → f2/f1 flush

// Send IBuffer
io.toIbuffer.bits = {instrs, valid, enqEnable, pd, pc, exceptionType, ...}
io.toIbuffer.valid = f3_valid && !mmio_state

// FTQ writeback
toFtq.pdWb.valid = ...
toFtq.pdWb.bits = {pc, pd, ftqIdx, misOffset, cfiOffset, target, jalTarget, instrRange}
```

### 4.2 flush propagation

```
backend_redirect = fromFtq.redirect.valid
f3_flush = backend_redirect || (wb_redirect && !f3_wb_not_flush)
f2_flush = backend_redirect || mmio_redirect || wb_redirect
f1_flush = f2_flush
f0_flush = f1_flush || f0_flush_from_bpu // Includes BPU S2/S3 override
```

### 4.3 Internal submodules

| Submodule | role |
| -------- | ---- |
| `PreDecode` | Finding instruction boundaries in 16-bit half-word streams, RVC status, and branch type primary determination |
| `F3Predecoder` | brType/isCall/isRet final decision in F3 |
| `PredChecker` | BPU prediction vs actual predecode comparison, IFU redirect generation |
| `FrontendTrigger` | HW trigger (debug) |
| `RVCExpander` × PredictWidth | 16-bit RVC → 32-bit expansion |

---

## 5. Functionality

### 5.1 Normal fetch flow

1. FTQ provides `FetchRequestBundle`(startAddr, ftqIdx, ftqOffset)
2. F0: Pass address to ICache (FTQ → ICache direct connection), IFU consumes req
3. F1: PC calculation (low-power adder isolation)
4. F2: Receive ICache response → Extract command data → predecode → Create exception
5. F3: RVC extension → PredChecker → IBuffer transfer + FTQ writeback

### 5.2 Cross-cacheline processing

- `f0_doubleLine = startAddr(blockOffBits-1) === 1` → Request the second cacheline as well
- ICache responds with two cacheline data in 5 bank × 8B format
- Duplicated twice with `f2_data_2_cacheline = Cat(data, data)` and extracted with `cut()` function

### 5.3 MMIO command processing

- `f2_pmp_mmio` detection in F2 → MMIO FSM operation in F3
- 64-bit unit request to InstrUncache → Extract 1 command from response
- After confirming ROB commit (`mmioLastCommit`), next MMIO fetch
- Backend redirect possible during MMIO → Immediate escape

### 5.4 PredChecker operation

- `jalFault`: It is a JAL command, but the target predicted by the BPU is different.
- `jalrFault`: JALR but target mismatch
- `retFault`: RET but target mismatch
- `targetFault`: Taken branch but target mismatch
- `notCFIFault`: Non-CFI instructions predicted to be taken
- `invalidTakenFault`: Instructions outside the fetch range are predicted to be taken
- When abnormality is detected → `wb_redirect = true` → IFU requests correction to FTQ

---

## 6. Flow / Backpressure Control

| Conditions | Action |
| ---- | ---- |
| IBuffer full (`!toIbuffer.ready`) | f3_fire=0, maintain f3_valid, `icacheStop=!f3_ready` → ICache stall |
| ICache not ready (`!icacheReady`) | `fromFtq.req.ready=0` → FTQ stall |
| ICache miss (`!icacheRespAllValid`) | f2_fire=0, maintain f2_valid → f1_ready=0 → FTQ stall |
| f2_icache_all_resp_reg | An ICache response was received, but F3 stall → stored in reg, preventing re-request |
| Backend redirect | f0~f3 all flush, f1/f2/f3_valid=0 |
| BPU S2/S3 flush | f0_flush_from_bpu → f0 flush (maintained after F1) |
| wb_redirect | f2/f3 flush (Keep F1), pass correction information to FTQ |
| MMIO redirect | f1/f2 flush, F3 occupied until MMIO completion |

---

## 7. Error / Exception Handling

| exception type | Detection point | Processing method |
| --------- | --------- | --------- |
| Page Fault (PF) | F2: `fromICache.bits.exception` (iTLB result) | `ExceptionType.pf` → FetchToIBuffer.exceptionType |
| Guest Page Fault (GPF) | F2: iTLB response | `ExceptionType.gpf` → Same path |
| Access Fault (AF) | F2: PMP/ECC/TileLink corrupt | `ExceptionType.af` → Same path |
| MMIO mismatch | F2: pmp_mmio / itlb_pbmt mismatch in double-line | AF occurs (shown in second line) |
| Cross-page exception | F2: Next page exception for last-in-line RVI instruction | `crossPage_exception_vec` → `IBufferExceptionType.CrossPF/GPF/AF` |
| Illegal RVC | F3: `expander.io.ill` | `IBufferExceptionType.rvcII` |

`ExceptionType.merge(iTLB_exception, mismatch_exception)` — Priority: iTLB > mismatch

---

## 8. Timing Hints

| Critical Path | Description |
| ------------- | ---- |
| F1: PC adder | `f1_pc_lower_result(i) = Cat(0,addr) + i*2` — PredictWidth parallel adder. Separate overflow processing with `CatPC` optimization |
| F2: ICache addr match | `fromICache.bits.vaddr(0) === f2_ftq_req.startAddr` — timing critical annotation (IFU.scala:365) |
| F2: cut() function | Vec(blockBytes, 16-bit) indexing × (PredictWidth+1) — big mux |
| F2: instr_range | `Fill(PW, ...) >> ~offset` — priority encoder equivalent |
| F3: PredChecker stage2 | Separate timing with 1-cycle delay. Complex comparison to checkerOutStage2 |
| F3: toIbuffer | DecodeWidth valid masking + exception type encoding parallel processing |

---

## 9. Pseudocode

```
F0 (combinational):
  if !f0_flush_from_bpu && fromFtq.req.valid && icacheReady:
    f0_fire = true
    (ICache already receives req from FTQ directly)

F1:
  if f0_fire && !f0_flush:
    f1_valid := true
    f1_ftq_req := f0_ftq_req
    f1_pc := CatPC(f1_pc_lower_result, high, high+1)
  if f1_flush:
    f1_valid := false
  f1_fire = f1_valid && f2_ready

F2:
  if f1_fire && !f1_flush:
    f2_valid := true
    f2_ftq_req := f1_ftq_req
    f2_pc := CatPC(f2_pc_lower_result, ...)
  wait icacheRespAllValid:
    f2_exception = ExceptionType.merge(itlb_exc, mmio_mismatch_exc)
    f2_cut_data = cut(Cat(icache_data, icache_data), f2_cut_ptr)
    preDecoder → f2_pd, f2_instr, f2_jump_offset
    f2_instr_range = jump_range & ftr_range
  if f2_flush:
    f2_valid := false; f2_icache_all_resp_reg := false
  f2_fire = f2_valid && f3_ready && icacheRespAllValid

F3:
  if f2_fire && !f2_flush:
    f3_valid := true
    // RVC expand, F3Predecoder, PredChecker
  if normal:
    io.toIbuffer.valid = true
    toFtq.pdWb.valid = true
    if wb_redirect:
      // flush f2/f3, send correction to FTQ
  if mmio:
    toUncache.valid = true
    wait fromUncache.valid
    wait mmioLastCommit
    mmio_redirect → f1/f2 flush
  if f3_flush:
    f3_valid := false
```

---

## 10. Notes / Assumptions

- `f3_wb_not_flush`: PredChecker detected redirect, but the result of F3 was delivered to IBuffer (non-flush condition)
- The IFU sends a stop signal (`icacheStop`) to ICache to stop the ICache main pipe when F3 is stalled and F2 is blocked.
- Determine TopDown stage trace array size with constant `numOfStage = 3`
- FTQ-ICache direct connection: `icache.io.fetch.req <> ftq.io.toICache.req` (no IFU in between), IFU only receives response
- `fetchQueueSize=2`: MMIO fetch internal buffer size (maximum 2 pending simultaneously)
