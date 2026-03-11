# frontend_Ftq_analysis.md

- Block: Frontend
- Module: Ftq
- Source: NewFtq.scala, FrontendBundle.scala
- Protocols: Decoupled (fromBpu.resp, toIfu.req, toICache.req, toPrefetch.req), Valid (toBpu.redirect/update, fromIfu.pdWb, fromBackend.redirect)
- Key Params: FtqSize=64, PredictWidth=16, copyNum=5, FtqRedirectAheadNum
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_in_out_seq_diagram.md](./frontend_in_out_seq_diagram.md#group-c)

---

## 1. Module Summary

- **Role**: A central queue that buffers BPU prediction results, supplies fetch requests to IFU / ICache / Prefetch in order, and updates/restores BPU by receiving IFU predecode results and Backend commit/redirect.
- **Position**: `Frontend.scala` → `Module(new Ftq)` (inside FrontendInlinedImp)
- **Number of pipeline stages**: No register stages (pointer-based circular queue) — However, 1 to 2 cycle SRAM latency exists between BPU enqueue and IFU transmission.

---

## 2. Key Parameters

| Parameter | Source | Default | Impact |
| ------------------ | ----------------------------------- | ------- | ----------------------------------------- |
| FtqSize | `p(XSCoreParamsKey).FtqSize` | 64 | Maximum number of in-flight fetch blocks (circular) |
| PredictWidth | `HasXSParameter.PredictWidth` | 16 | number of slots per commitStateQueue entry |
| copyNum | `Ftq.copyNum` | 5 | Number of pointer replicas (reduces fanout) |
| FtqRedirectAheadNum| `HasXSParameter` | ~4 | BjuCnt based redirect ahead port number |
| IfuRedirectNum | `HasXSParameter` | 1 | IFU redirect SRAM read port number |

---

## 3. Interfaces

| Port                    | Dir   | Bitwidth          | Protocol  | Description                              |
| ----------------------- | ----- | ----------------- | --------- | ---------------------------------------- |
| `io.fromBpu.resp` | in | BpuToFtqBundle | Decoupled | BPU prediction results (including s1/s2/s3) |
| `io.toBpu.redirect`     | out   | BranchPredictionRedirect | Valid | misprediction → BPU redirect          |
| `io.toBpu.update` | out | BranchPredictionUpdate | Valid | commit information → BPU learning |
| `io.toBpu.enq_ptr` | out | FtqPtr | — | Pass current bpuPtr |
| `io.toBpu.redirctFromIFU`| out | Bool | — | IFU redirect or not |
| `io.fromIfu.pdWb`       | in    | PredecodeWritebackBundle | Valid | IFU predecode writeback               |
| `io.toIfu.req` | out | FetchRequestBundle | Decoupled | fetch request with IFU |
| `io.toIfu.redirect`     | out   | BranchPredictionRedirect | Valid | IFU flush                             |
| `io.toIfu.flushFromBpu` | out | BpuFlushInfo | — | BPU S2/S3 override flush information |
| `io.toICache.req` | out | FtqToICacheRequestBundle | Decoupled | ICache fetch request (5 copies) |
| `io.toPrefetch.req` | out | FtqICacheInfo | Decoupled | Prefetch request |
| `io.fromBackend` | in | CtrlToFtqIO | — | redirect, rob_commits, ftqIdxAhead, etc. |
| `io.toBackend` | out | FtqToCtrlIO | — | pc_mem write, newest_entry info |
| `io.mmioCommitRead` | in/out| mmioCommitRead | — | IFU MMIO commit confirmation |
| `io.icacheFlush` | out | Bool | — | ICache pipe flush |
| `io.bpuInfo` | out | Bundle | — | BPU hit/misprediction counter |

---

## 4. Internal Pipeline / State

### 4.1 Pointer structure (all `RegInit(FtqPtr(false.B, 0.U))`)

| pointer | role | Increase conditions |
| ------- | ---- | --------- |
| `bpuPtr` | BPU new entry enqueue location | `enq_fire` (BPU s1 response fire) |
| `ifuPtr` | Next entry to transfer to IFU | `toIfu.req.fire && allowToIfu` |
| `ifuPtrPlus1/2` | precompute ifuPtr+1/+2 | Same |
| `pfPtr` | Prefetch transfer pointer | `toPrefetch.req.fire` |
| `ifuWbPtr` | IFU predecode writeback confirmation pointer | `ifu_wb_valid` |
| `commPtr` | commit completion pointer | `canCommit` |
| `commPtrPlus1` | commPtr+1 precompute | Same |
| `robCommPtr` | ROB commit synchronous pointer | ROB commit |

Pointer invariant: `commPtr ≤ ifuWbPtr ≤ ifuPtr ≤ bpuPtr`

### 4.2 Internal SRAM/Register Structure

| name | Type | size | role |
| ---- | ---- | ---- | ---- |
| `ftq_pc_mem` (FtqPcMemWrapper) | SyncDataModuleTemplate | FtqSize × Ftq_RF_Components | PC/nextLine storage, multiple read ports |
| `ftq_redirect_mem` | SyncDataModuleTemplate | FtqSize × Ftq_Redirect_SRAMEntry | Branch History/RAS Status (for redirect restore) |
| `ftq_meta_mem` | SyncDataModuleTemplate | FtqSize × MetaEntry | BPU metadata + FTBEntry (for training) |
| `ftq_pd_mem` | SyncDataModuleTemplate | FtqSize × Ftq_pd_Entry | IFU predecode result (brMask, jmpInfo) |
| `ftb_entry_mem` | SyncDataModuleTemplate | FtqSize × FTBEntry_FtqMem | FTB entry (for redirect verification) |
| `update_target` | Reg Vec | FtqSize × VAddrBits | Predicted target address |
| `cfiIndex_vec` | Reg Vec | FtqSize × ValidUInt | CFI Position Index |
| `mispredict_vec` | Reg Vec | FtqSize × Vec(PW, Bool) | Whether each slot has misprediction |
| `pred_stage` | Reg Vec | FtqSize × 2 | prediction stage (S1/S2/S3) |
| `commitStateQueueReg` | RegInit Vec | FtqSize × Vec(PW, 2bit) | Commit status by command (empty/toCommit/committed/flushed) |
| `entry_fetch_status` | RegInit Vec | FtqSize × 1bit | f_to_send / f_sent |
| `entry_hit_status` | RegInit Vec | FtqSize × 2bit | h_not_hit / h_false_hit / h_hit |

### 4.3 Replication structure (reduce fanout)

- `copied_ifu_ptr[5]`, `copied_bpu_ptr[5]`: 5 copies of ifuPtr/bpuPtr
- `copied_bpu_in_bypass_buf[5]`, `copied_bpu_in_bypass_ptr[5]`: bypass data replication
- Connect independent signals to each of the five ICache read ports with `copyNum=5`

### 4.4 BPU enqueue 1-cycle delay

```
// Critical path mitigation: Register update after 1 cycle of BPU enqueue
last_cycle_bpu_in       = RegNext(bpu_in_fire)
last_cycle_bpu_in_ptr   = RegEnable(bpu_in_resp_ptr, bpu_in_fire)
last_cycle_bpu_target   = RegEnable(bpu_in_resp.getTarget(3), bpu_in_fire)
last_cycle_cfiIndex     = RegEnable(bpu_in_resp.cfiIndex(3), bpu_in_fire)
// Update entry_fetch_status, cfiIndex_vec, and update_target in the next cycle
```

### 4.5 bypass route (BPU enqueue → IFU transmission 0-cycle)

```
// SRAM bypass when the entry just enqueued by BPU matches ifuPtr
when(last_cycle_bpu_in && bpu_in_bypass_ptr === ifuPtr):
  toIfuPcBundle    := bpu_in_bypass_buf_for_ifu
  entry_is_to_send := true.B
  entry_next_addr  := last_cycle_bpu_target
```

---

## 5. Functionality

### 5.1 BPU enqueue (fromBpu → FTQ)

1. `io.fromBpu.resp.fire && allowBpuIn` → `enq_fire` → `bpuPtr += 1`
2. `bpu_in_resp = selectedResp` (S3 redirect > S2 redirect > S1)
3. SRAM write (after 1 cycle): `ftq_pc_mem`, `ftq_redirect_mem` (lastStage), `ftq_meta_mem` (lastStage)
4. Rewind `bpuPtr` when S2/S3 redirect: `bpuPtr := bpu_s2_resp.ftq_idx + 1`

### 5.2 IFU/ICache transfer (FTQ → IFU/ICache)

1. `entry_is_to_send = (entry_fetch_status(ifuPtr) === f_to_send) || bypass`
2. `io.toIfu.req.valid = entry_is_to_send && ifuPtr ≠ bpuPtr`
3. `io.toIfu.req.fire && allowToIfu` → `ifuPtr += 1`, `entry_fetch_status := f_sent`
4. Simultaneous transmission of 5 copies to ICache (toICachePcBundle/toICacheEntryToSend)
5. `allowToIfu = !ifuFlush && !backendRedirect && !backendRedirectReg`

### 5.3 IFU predecode writeback (fromIfu → FTQ)

1. `ifu_wb_valid = pdWb.valid` → `ftq_pd_mem` write
2. `commitStateQueueNext`: `inRange && valid` slot `c_empty → c_toCommit`
3. `ifuWbPtr += 1`
4. FTB hit, mispred detected → `has_false_hit` → false_hit processed, `hit_pd_mispred` recorded
5. When IFU redirect occurs → `toBpu.redirect.valid` (Send IFU modification information)

### 5.4 Commit processing (fromBackend → FTQ)

1. `canCommit`: When all `c_toCommit` slots in the commPtr entry are committed or empty.
2. Update `mispredict_vec` with the actual result of ROB when committing
3. `FTBEntryGen`: Create a new FTB entry with predecode + commit information
4. `toBpu.update.valid` → BPU training data transfer (pc, ftb_entry, br_taken_mask, mispred_mask, etc.)
5. `commPtr += 1`

### 5.5 Backend redirect (fromBackend → FTQ)

1. `backendRedirect.valid` → `stage2Flush = true`
2. `backendFlush = stage2Flush || RegNext(stage2Flush)` (2 cycles)
3. `allowBpuIn = allowToIfu = false` (flush period)
4. `bpuPtr, ifuPtr, pfPtr` → redirect target-based restoration
5. `toBpu.redirect.valid` → BPU history restoration
6. `icacheFlush` → Invalidate ICache pipe
7. `toIfu.redirect.valid` → IFU flush

### 5.6 BPU S2/S3 override

```
// S2 redirect
when(bpu_s2_redirect):
  bpuPtr := bpu_s2_resp.ftq_idx + 1
  if ifuPtr > bpu_s2_resp.ftq_idx:
    ifuPtr := bpu_s2_resp.ftq_idx
  toIfu.flushFromBpu.s2 := {valid=true, bits=ftq_idx}

// S3 redirect
when(bpu_s3_redirect):
  bpuPtr := bpu_s3_resp.ftq_idx + 1
  if ifuPtr > bpu_s3_resp.ftq_idx:
    ifuPtr := bpu_s3_resp.ftq_idx
  toIfu.flushFromBpu.s3 := {valid=true, bits=ftq_idx}
```

### 5.7 FTQ full detection

- `validEntries = distanceBetween(bpuPtr, commPtr)` ≥ FtqSize → `new_entry_ready = false`
- `io.fromBpu.resp.ready := new_entry_ready` → BPU back-pressure

---

## 6. Flow / Backpressure Control

| Conditions | Action |
| ---- | ---- |
| FTQ full | `io.fromBpu.resp.ready = false` → BPU stall |
| backend redirect | `allowBpuIn/ToIfu = false` (2 cycles), restore bpuPtr/ifuPtr |
| BPU s2/s3 redirect | Rewind bpuPtr, rewind ifuPtr (if needed), pass flushFromBpu |
| IFU not ready | `toIfu.req.ready = f1_ready && icacheReady` → do not proceed with ifuPtr |
| entry not f_to_send | `entry_is_to_send = false` → toIfu.req.valid = false |
| ifuFlush | `allowToIfu = false` → Block IFU request |
| MMIO commit wait | IFU control based on `mmioCommitRead.valid` + `mmioLastCommit` |

---

## 7. Error / Exception Handling

| Situation | processing |
| ---- | ---- |
| Backend IPF/IGF/IAF | Write register `backendException`, `backendPcFaultPtr = ifuWbPtr` |
| throwing ICache backendException | `toICache.req.bits.backendException = backendPcFaultPtr === ifuPtr` |
| IFU predecode mismatch (false hit) | `entry_hit_status := h_false_hit`, pass false_hit flag to BPU |
| fallThroughError | `entry_hit_status := h_false_hit` (FTB ghost entry detection) |
| FTBEntryGen mispred | Create `mispred_mask` → `toBpu.update.bits.mispred_mask` |
| `XSError` invariant check | commPtr ≤ ifuWbPtr ≤ ifuPtr ≤ bpuPtr, IFU wb order, etc. |

---

## 8. Timing Hints

| Critical Path | Description |
| ------------- | ---- |
| `entry_is_to_send` | `entry_fetch_status(ifuPtr.value) === f_to_send` — SRAM/register index with ifuPtr |
| `validEntries` | `distanceBetween(bpuPtr, commPtr)` — CircularQueuePtr distance calculation, directly connected to `io.fromBpu.resp.ready` |
| `toIfuPcBundle` mux | bypass / last_cycle_to_ifu_fire / otherwise 3-way mux — with SRAM rdata latency |
| `commitStateQueueReg` | FtqSize × PredictWidth 2-bit register — Reduce fanout by splitting copyNum |
| backend redirect ahead | `ftqIdxAhead / ftqIdxSelOH`: Advance the redirect cycle by 1 cycle `realAhdValid` |
| `FTBEntryGen` | combinational: br slot insertion/move logic, pftAddr calculation |

---

## 9. Pseudocode

```
// BPU enqueue
on io.fromBpu.resp.fire && allowBpuIn:
  bpuPtr += 1
  ftq_pc_mem.write(bpu_in_resp_ptr, fromBranchPrediction(bpu_in_resp))
  [+1 cycle] entry_fetch_status(idx) := f_to_send
             cfiIndex_vec(idx) := last_cycle_cfiIndex
             update_target(idx) := last_cycle_bpu_target

// to IFU
each cycle:
  if entry_is_to_send && ifuPtr ≠ bpuPtr:
    toIfu.req.valid = true
  if toIfu.req.fire && allowToIfu:
    ifuPtr += 1
    entry_fetch_status(ifuPtr_old) := f_sent

// IFU writeback
on ifu_wb_valid:
  ftq_pd_mem.write(idx, pd)
  commitStateQueue(idx)[valid && inRange] := c_toCommit
  ifuWbPtr += 1
  if hit && misOffset.valid:
has_false_hit = true → BPU update (false hit processing)

// Commit
when canCommit:
  FTBEntryGen(pd, cfiIndex, target, hit) → new_entry
  toBpu.update = {pc, ftb_entry=new_entry, br_taken_mask, mispred_mask, ...}
  commPtr += 1

// Backend redirect
on fromBackend.redirect.valid:
  stage2Flush = true  // → allowBpuIn/ToIfu = false
Restore bpuPtr, ifuPtr, pfPtr
toBpu.redirect.valid = true // Restore BPU history
  toIfu.redirect.valid = true  // IFU flush
  icacheFlush = true
[+1 cycle] backendFlush=true (lasts 2 cycles)

// BPU S2/S3 override
on bpu_s2_redirect:
  bpuPtr := s2_ftq_idx + 1
  if ifuPtr > s2_ftq_idx: ifuPtr := s2_ftq_idx
  flushFromBpu.s2 = {valid, s2_ftq_idx}
```

---

## 10. Notes / Assumptions

- `copyNum=5`: Register replication for ICache 5-port and BPU pointer fanout distribution
- `last_cycle_bpu_in` 1-cycle delay: BPU enqueue critical path blocked — `entry_fetch_status` update occurs in the next enqueue cycle, so bypass logic is required
- `bpu_in_bypass_buf`: PC enqueued by BPU is supplied directly to IFU without going through SRAM (1-entry bypass)
- `validEntries < FtqSize || canCommit`: Allow enqueue if commit occurs even under FTQ full condition (prevent deadlock)
- `FtqRedirectAheadNum`: Backend announces redirect 1 cycle earlier (`ftqIdxAhead`) → Redirect latency is reduced to `realAhdValid`
- `backendException` Register: Maintains IPF/IAF information received from Backend until IFU writeback is completed — Exception is confirmed only when IFU writes back the entry
