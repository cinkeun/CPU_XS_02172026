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

- **역할**: BPU 예측 결과를 버퍼링하고 IFU / ICache / Prefetch에 fetch 요청을 순서대로 공급하며, IFU predecode 결과와 Backend commit/redirect를 수신해 BPU를 업데이트/복원하는 중앙 큐이다.
- **위치**: `Frontend.scala` → `Module(new Ftq)` (FrontendInlinedImp 내부)
- **Pipeline stage 수**: 레지스터 스테이지 없음 (포인터 기반 circular queue) — 단, BPU enqueue와 IFU 전송 사이에 1~2 사이클 SRAM latency 존재

---

## 2. Key Parameters

| Parameter          | Source                              | Default | 영향                                      |
| ------------------ | ----------------------------------- | ------- | ----------------------------------------- |
| FtqSize            | `p(XSCoreParamsKey).FtqSize`        | 64      | 최대 in-flight fetch 블록 수 (circular)   |
| PredictWidth       | `HasXSParameter.PredictWidth`       | 16      | commitStateQueue 엔트리당 슬롯 수          |
| copyNum            | `Ftq.copyNum`                       | 5       | 포인터 복제본 수 (fanout 감소)             |
| FtqRedirectAheadNum| `HasXSParameter`                    | ~4      | BjuCnt 기반 redirect ahead 포트 수        |
| IfuRedirectNum     | `HasXSParameter`                    | 1       | IFU redirect SRAM 읽기 포트 수            |

---

## 3. Interfaces

| Port                    | Dir   | Bitwidth          | Protocol  | Description                              |
| ----------------------- | ----- | ----------------- | --------- | ---------------------------------------- |
| `io.fromBpu.resp`       | in    | BpuToFtqBundle    | Decoupled | BPU 예측 결과 (s1/s2/s3 포함)            |
| `io.toBpu.redirect`     | out   | BranchPredictionRedirect | Valid | misprediction → BPU redirect          |
| `io.toBpu.update`       | out   | BranchPredictionUpdate   | Valid | commit 정보 → BPU 학습                 |
| `io.toBpu.enq_ptr`      | out   | FtqPtr            | —         | 현재 bpuPtr 전달                         |
| `io.toBpu.redirctFromIFU`| out  | Bool              | —         | IFU redirect 여부                        |
| `io.fromIfu.pdWb`       | in    | PredecodeWritebackBundle | Valid | IFU predecode writeback               |
| `io.toIfu.req`          | out   | FetchRequestBundle | Decoupled | IFU로 fetch 요청                        |
| `io.toIfu.redirect`     | out   | BranchPredictionRedirect | Valid | IFU flush                             |
| `io.toIfu.flushFromBpu` | out   | BpuFlushInfo      | —         | BPU S2/S3 override flush 정보            |
| `io.toICache.req`       | out   | FtqToICacheRequestBundle | Decoupled | ICache fetch 요청 (5 copies)        |
| `io.toPrefetch.req`     | out   | FtqICacheInfo     | Decoupled | Prefetch 요청                            |
| `io.fromBackend`        | in    | CtrlToFtqIO       | —         | redirect, rob_commits, ftqIdxAhead 등    |
| `io.toBackend`          | out   | FtqToCtrlIO       | —         | pc_mem write, newest_entry 정보          |
| `io.mmioCommitRead`     | in/out| mmioCommitRead    | —         | IFU MMIO commit 확인                     |
| `io.icacheFlush`        | out   | Bool              | —         | ICache 파이프 flush                      |
| `io.bpuInfo`            | out   | Bundle            | —         | BPU 적중/오예측 카운터                    |

---

## 4. Internal Pipeline / State

### 4.1 포인터 구조 (모두 `RegInit(FtqPtr(false.B, 0.U))`)

| 포인터 | 역할 | 증가 조건 |
| ------- | ---- | --------- |
| `bpuPtr` | BPU 새 엔트리 enqueue 위치 | `enq_fire` (BPU s1 응답 fire) |
| `ifuPtr` | IFU로 전송할 다음 엔트리 | `toIfu.req.fire && allowToIfu` |
| `ifuPtrPlus1/2` | ifuPtr+1/+2 미리 계산 | 동일 |
| `pfPtr` | Prefetch 전송 포인터 | `toPrefetch.req.fire` |
| `ifuWbPtr` | IFU predecode writeback 확인 포인터 | `ifu_wb_valid` |
| `commPtr` | commit 완료 포인터 | `canCommit` |
| `commPtrPlus1` | commPtr+1 미리 계산 | 동일 |
| `robCommPtr` | ROB commit 동기 포인터 | ROB commit |

포인터 불변식: `commPtr ≤ ifuWbPtr ≤ ifuPtr ≤ bpuPtr`

### 4.2 내부 SRAM / 레지스터 구조

| 이름 | 타입 | 크기 | 역할 |
| ---- | ---- | ---- | ---- |
| `ftq_pc_mem` (FtqPcMemWrapper) | SyncDataModuleTemplate | FtqSize × Ftq_RF_Components | PC/nextLine 저장, 다수 read 포트 |
| `ftq_redirect_mem` | SyncDataModuleTemplate | FtqSize × Ftq_Redirect_SRAMEntry | 분기 이력/RAS 상태 (redirect 복원용) |
| `ftq_meta_mem` | SyncDataModuleTemplate | FtqSize × MetaEntry | BPU 메타데이터 + FTBEntry (학습용) |
| `ftq_pd_mem` | SyncDataModuleTemplate | FtqSize × Ftq_pd_Entry | IFU predecode 결과 (brMask, jmpInfo) |
| `ftb_entry_mem` | SyncDataModuleTemplate | FtqSize × FTBEntry_FtqMem | FTB entry (redirect 검증용) |
| `update_target` | Reg Vec | FtqSize × VAddrBits | 예측 target 주소 |
| `cfiIndex_vec` | Reg Vec | FtqSize × ValidUInt | CFI 위치 인덱스 |
| `mispredict_vec` | Reg Vec | FtqSize × Vec(PW, Bool) | 각 슬롯 misprediction 여부 |
| `pred_stage` | Reg Vec | FtqSize × 2 | 예측 stage (S1/S2/S3) |
| `commitStateQueueReg` | RegInit Vec | FtqSize × Vec(PW, 2bit) | 명령어별 commit 상태 (empty/toCommit/committed/flushed) |
| `entry_fetch_status` | RegInit Vec | FtqSize × 1bit | f_to_send / f_sent |
| `entry_hit_status` | RegInit Vec | FtqSize × 2bit | h_not_hit / h_false_hit / h_hit |

### 4.3 복제 구조 (fanout 감소)

- `copied_ifu_ptr[5]`, `copied_bpu_ptr[5]`: ifuPtr/bpuPtr 복제 5벌
- `copied_bpu_in_bypass_buf[5]`, `copied_bpu_in_bypass_ptr[5]`: bypass 데이터 복제
- `copyNum=5`로 ICache 5개 read port에 각각 독립 신호 연결

### 4.4 BPU enqueue 1-cycle delay

```
// critical path 완화: BPU enqueue 1사이클 후 레지스터 갱신
last_cycle_bpu_in       = RegNext(bpu_in_fire)
last_cycle_bpu_in_ptr   = RegEnable(bpu_in_resp_ptr, bpu_in_fire)
last_cycle_bpu_target   = RegEnable(bpu_in_resp.getTarget(3), bpu_in_fire)
last_cycle_cfiIndex     = RegEnable(bpu_in_resp.cfiIndex(3), bpu_in_fire)
// 다음 사이클에 entry_fetch_status, cfiIndex_vec, update_target 갱신
```

### 4.5 bypass 경로 (BPU enqueue → IFU 전송 0-cycle)

```
// BPU가 방금 enqueue한 엔트리가 ifuPtr와 일치할 때 SRAM bypass
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
3. SRAM write (1 cycle 후): `ftq_pc_mem`, `ftq_redirect_mem` (lastStage), `ftq_meta_mem` (lastStage)
4. S2/S3 redirect 시 `bpuPtr` 되감기: `bpuPtr := bpu_s2_resp.ftq_idx + 1`

### 5.2 IFU / ICache 전송 (FTQ → IFU/ICache)

1. `entry_is_to_send = (entry_fetch_status(ifuPtr) === f_to_send) || bypass`
2. `io.toIfu.req.valid = entry_is_to_send && ifuPtr ≠ bpuPtr`
3. `io.toIfu.req.fire && allowToIfu` → `ifuPtr += 1`, `entry_fetch_status := f_sent`
4. ICache에는 5 copies 동시 전송 (toICachePcBundle/toICacheEntryToSend)
5. `allowToIfu = !ifuFlush && !backendRedirect && !backendRedirectReg`

### 5.3 IFU predecode writeback (fromIfu → FTQ)

1. `ifu_wb_valid = pdWb.valid` → `ftq_pd_mem` write
2. `commitStateQueueNext`: `inRange && valid` 슬롯을 `c_empty → c_toCommit`
3. `ifuWbPtr += 1`
4. FTB hit인데 mispred 감지 → `has_false_hit` → false_hit 처리, `hit_pd_mispred` 기록
5. IFU redirect 발생 시 → `toBpu.redirect.valid` (IFU 수정 정보 전달)

### 5.4 Commit 처리 (fromBackend → FTQ)

1. `canCommit`: commPtr 엔트리의 모든 `c_toCommit` 슬롯이 committed 또는 비었을 때
2. commit 시 ROB의 실제 결과로 `mispredict_vec` 갱신
3. `FTBEntryGen`: predecode + commit 정보로 새 FTB entry 생성
4. `toBpu.update.valid` → BPU 학습 데이터 전달 (pc, ftb_entry, br_taken_mask, mispred_mask 등)
5. `commPtr += 1`

### 5.5 Backend redirect (fromBackend → FTQ)

1. `backendRedirect.valid` → `stage2Flush = true`
2. `backendFlush = stage2Flush || RegNext(stage2Flush)` (2 사이클)
3. `allowBpuIn = allowToIfu = false` (flush 기간)
4. `bpuPtr, ifuPtr, pfPtr` → redirect target 기반 복원
5. `toBpu.redirect.valid` → BPU 이력 복원
6. `icacheFlush` → ICache 파이프 무효화
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

### 5.7 FTQ full 감지

- `validEntries = distanceBetween(bpuPtr, commPtr)` ≥ FtqSize → `new_entry_ready = false`
- `io.fromBpu.resp.ready := new_entry_ready` → BPU back-pressure

---

## 6. Flow / Backpressure Control

| 조건 | 동작 |
| ---- | ---- |
| FTQ full | `io.fromBpu.resp.ready = false` → BPU stall |
| backend redirect | `allowBpuIn/ToIfu = false` (2 사이클), bpuPtr/ifuPtr 복원 |
| BPU s2/s3 redirect | bpuPtr 되감기, ifuPtr 되감기 (필요 시), flushFromBpu 전달 |
| IFU not ready | `toIfu.req.ready = f1_ready && icacheReady` → ifuPtr 진행 안 함 |
| entry not f_to_send | `entry_is_to_send = false` → toIfu.req.valid = false |
| ifuFlush | `allowToIfu = false` → IFU 요청 차단 |
| MMIO commit wait | `mmioCommitRead.valid` + `mmioLastCommit` 기반 IFU 제어 |

---

## 7. Error / Exception Handling

| 상황 | 처리 |
| ---- | ---- |
| Backend IPF/IGF/IAF | `backendException` 레지스터 기록, `backendPcFaultPtr = ifuWbPtr` |
| ICache backendException 전달 | `toICache.req.bits.backendException = backendPcFaultPtr === ifuPtr` |
| IFU predecode mismatch (false hit) | `entry_hit_status := h_false_hit`, BPU에 false_hit 플래그 전달 |
| fallThroughError | `entry_hit_status := h_false_hit` (FTB ghost entry 감지) |
| FTBEntryGen mispred | `mispred_mask` 생성 → `toBpu.update.bits.mispred_mask` |
| `XSError` 불변식 검사 | commPtr ≤ ifuWbPtr ≤ ifuPtr ≤ bpuPtr, IFU wb 순서 등 |

---

## 8. Timing Hints

| Critical Path | 설명 |
| ------------- | ---- |
| `entry_is_to_send` | `entry_fetch_status(ifuPtr.value) === f_to_send` — ifuPtr로 SRAM/레지스터 인덱스 |
| `validEntries` | `distanceBetween(bpuPtr, commPtr)` — CircularQueuePtr 거리 계산, `io.fromBpu.resp.ready`에 직결 |
| `toIfuPcBundle` mux | bypass / last_cycle_to_ifu_fire / otherwise 3-way mux — SRAM rdata latency 포함 |
| `commitStateQueueReg` | FtqSize × PredictWidth 2-bit 레지스터 — copyNum 분할로 팬아웃 감소 |
| backend redirect ahead | `ftqIdxAhead / ftqIdxSelOH`: redirect cycle을 1 사이클 앞당겨 `realAhdValid` |
| `FTBEntryGen` | combinational: br slot 삽입/이동 로직, pftAddr 계산 |

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
    has_false_hit = true → BPU update (false hit 처리)

// Commit
when canCommit:
  FTBEntryGen(pd, cfiIndex, target, hit) → new_entry
  toBpu.update = {pc, ftb_entry=new_entry, br_taken_mask, mispred_mask, ...}
  commPtr += 1

// Backend redirect
on fromBackend.redirect.valid:
  stage2Flush = true  // → allowBpuIn/ToIfu = false
  bpuPtr, ifuPtr, pfPtr 복원
  toBpu.redirect.valid = true  // BPU 이력 복원
  toIfu.redirect.valid = true  // IFU flush
  icacheFlush = true
  [+1 cycle] backendFlush=true (2 사이클 지속)

// BPU S2/S3 override
on bpu_s2_redirect:
  bpuPtr := s2_ftq_idx + 1
  if ifuPtr > s2_ftq_idx: ifuPtr := s2_ftq_idx
  flushFromBpu.s2 = {valid, s2_ftq_idx}
```

---

## 10. Notes / Assumptions

- `copyNum=5`: ICache 5포트 및 BPU 포인터 팬아웃 분산을 위해 레지스터 복제
- `last_cycle_bpu_in` 1-사이클 지연: BPU enqueue critical path 차단 — `entry_fetch_status` 갱신이 enqueue 다음 사이클에 발생하므로 bypass 로직 필요
- `bpu_in_bypass_buf`: BPU가 enqueue한 PC를 SRAM을 거치지 않고 직접 IFU에 공급 (1-entry bypass)
- `validEntries < FtqSize || canCommit`: FTQ full 조건에서도 commit이 발생하면 enqueue 허용 (deadlock 방지)
- `FtqRedirectAheadNum`: Backend가 redirect를 1 사이클 앞당겨 예고(`ftqIdxAhead`) → `realAhdValid`로 redirect latency 단축
- `backendException` 레지스터: Backend로부터 받은 IPF/IAF 정보를 IFU writeback 완료 전까지 유지 — IFU가 해당 엔트리를 writeback해야 예외가 확정됨
