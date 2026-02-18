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

- **역할**: FTQ가 제공하는 fetch 주소로 ICache에서 명령어를 가져와 predecode 후 IBuffer로 전달한다. MMIO 명령어는 InstrUncache를 통해 별도 처리한다.
- **위치**: `Frontend.scala` → `Module(new NewIFU)` (FrontendInlinedImp 내부)
- **Pipeline stage 수**: 3 register stages (F1/F2/F3) + F0 launch phase

---

## 2. Key Parameters

| Parameter     | Source                          | Default | 영향                                |
| ------------- | ------------------------------- | ------- | ----------------------------------- |
| PredictWidth  | `HasXSParameter.PredictWidth`   | 16      | fetch block당 최대 half-word 수      |
| HasCExtension | `HasXSParameter.HasCExtension`  | true    | RVC 명령어 지원 여부 (16-bit 처리)   |
| CommitWidth   | `p(XSCoreParamsKey).CommitWidth`| 6       | rob_commits 포트 수                 |
| fetchQueueSize| `HasIFUConst.fetchQueueSize`    | 2       | MMIO fetch 내부 큐 크기             |
| blockOffBits  | `HasICacheParameters`           | 6       | 캐시라인 offset bits (64B → 6bits)  |
| mmioBusWidth  | `HasInstrMMIOConst`             | 64      | MMIO fetch 버스 폭 (bits)           |
| VAddrBits     | `HasXSParameter`                | 39+     | 가상 주소 비트 수                   |

---

## 3. Interfaces

| Port                        | Dir | Bitwidth       | Protocol   | Description                           |
| --------------------------- | --- | -------------- | ---------- | ------------------------------------- |
| `io.ftqInter.fromFtq.req`   | in  | FetchRequestBundle | Decoupled | FTQ → IFU fetch 요청 |
| `io.ftqInter.fromFtq.redirect` | in | BranchPredictionRedirect | Valid | backend redirect |
| `io.ftqInter.fromFtq.flushFromBpu` | in | BpuFlushInfo | — | BPU S2/S3 flush 정보 |
| `io.ftqInter.toFtq.pdWb`   | out | PredecodeWritebackBundle | Valid | predecode 결과 → FTQ |
| `io.icacheInter.resp`       | in  | ICacheMainPipeResp | ValidIO | ICache → IFU 응답 |
| `io.icacheInter.icacheReady`| in  | Bool           | —          | ICache 준비 여부 (F0 req.ready 조건) |
| `io.icacheStop`             | out | Bool           | —          | ICache 정지 요청 (`!f3_ready`)        |
| `io.toIbuffer`              | out | FetchToIBuffer | Decoupled  | IFU → IBuffer 명령어 전달             |
| `io.toBackend`              | out | IfuToBackendIO | —          | IFU → Backend gpaddr 기록             |
| `io.uncacheInter.toUncache` | out | InsUncacheReq  | Decoupled  | MMIO fetch 요청                       |
| `io.uncacheInter.fromUncache`| in | InsUncacheResp | Decoupled  | MMIO fetch 응답                       |
| `io.iTLBInter`              | in/out | TlbRequestIO | —        | MMIO 재변환용 iTLB (block 가능)       |
| `io.pmp`                    | in/out | ICachePMPBundle | —      | PMP 검사 (IFU용 마지막 포트)          |
| `io.mmioCommitRead`         | out | mmioCommitRead | —          | MMIO FTQ 포인터 / lastCommit 확인    |
| `io.rob_commits`            | in  | Vec(CommitWidth, Valid) | — | ROB commit 정보 (MMIO 제어) |
| `io.frontendTrigger`        | in  | FrontendTdataDistributeIO | — | 디버그 트리거 설정 |
| `io.csr_fsIsOff`            | in  | Bool           | —          | FS 비활성화 여부 (RVC illegal 판단)   |

---

## 4. Internal Pipeline / State

### 4.1 Stage 상세

#### F0 — Launch Phase (combinational)
```
fromFtq.req.valid → f0_valid
fromFtq.req.bits  → f0_ftq_req (startAddr, nextlineStart, ftqIdx, ftqOffset)
f0_doubleLine = startAddr(blockOffBits-1) === 1  // cross-cacheline 여부
f0_vSetIdx = Vec(get_idx(startAddr), get_idx(nextlineStart))
f0_fire = fromFtq.req.fire = f0_valid && f1_ready && icacheReady
f0_flush_from_bpu = shouldFlushByStage2(f0_ftq_req.ftqIdx) || shouldFlushByStage3(...)
```

#### F1 — PC 계산 (1 register stage)
```
f1_valid = RegInit(false.B)
f1_ftq_req = RegEnable(f0_ftq_req, f0_fire)
f1_doubleLine = RegEnable(f0_doubleLine, f0_fire)
f1_vSetIdx = RegEnable(f0_vSetIdx, f0_fire)
f1_fire = f1_valid && f2_ready

// PC adder 최적화 (PcCutPoint = VAddrBits/4 - 1)
f1_pc_high = f1_ftq_req.startAddr(VAddrBits-1, PcCutPoint)
f1_pc_lower_result(i) = Cat(0.U(1.W), startAddr(PcCutPoint-1, 0)) + (i*2).U  // overflow bit 포함
f1_pc = CatPC(f1_pc_lower_result, f1_pc_high, f1_pc_high_plus1)

f1_cut_ptr(i) = startAddr(blockOffBits-1, 1) + i  // ICache data 자르기 포인터
```

#### F2 — ICache 응답 처리 (1 register stage)
```
f2_valid = RegInit(false.B)
f2_ftq_req = RegEnable(f1_ftq_req, f1_fire)
f2_doubleLine = RegEnable(f1_doubleLine, f1_fire)
f2_pc = CatPC(RegEnable(f1_pc_lower_result), ...)
f2_fire = f2_valid && f3_ready && icacheRespAllValid

// ICache 응답 유효성 확인 (timing critical)
f2_icache_all_resp_wire =
  fromICache.valid &&
  fromICache.bits.vaddr(0) === f2_ftq_req.startAddr &&
  (!f2_doubleLine || fromICache.bits.vaddr(1) === f2_ftq_req.nextlineStart)

// data 처리: 5 bank × 8B = 40B 응답을 2배 복제 후 중간 추출
f2_data_2_cacheline = Cat(fromICache.bits.data, fromICache.bits.data)
f2_cut_data = cut(f2_data_2_cacheline, f2_cut_ptr)  // PredictWidth+1 개 16-bit 조각

// 예외 생성
f2_exception = ExceptionType.merge(f2_exception_in, f2_mmio_mismatch_exception)
f2_exception_vec(i) = 각 pc(i)가 속한 cacheline의 예외

// Predecode (combinational, driven by f2_valid)
preDecoder.in = {f2_cut_data, f2_pc, frontendTrigger}
f2_pd = preDecoder.out.pd      // PreDecodeInfo per instruction
f2_instr = preDecoder.out.instr
f2_jump_offset = preDecoder.out.jumpOffset

// 명령어 범위
f2_jump_range = Fill(PW, !ftqOffset.valid) | Fill(PW,1)>>~ftqOffset.bits
f2_ftr_range  = Fill(PW, ftqOffset.valid) | Fill(PW,1)>>~getBasicBlockIdx(nextStartAddr,startAddr)
f2_instr_range = f2_jump_range & f2_ftr_range
```

#### F3 — IBuffer 전송 (1 register stage)
```
f3_valid = RegInit(false.B)
f3_ftq_req = RegEnable(f2_ftq_req, f2_fire)
f3_fire = io.toIbuffer.fire

// RVC 확장
expanders(i): RVCExpander
f3_expd_instr(i) = Mux(expander.io.ill, original, expander.io.out.bits)
f3_ill(i) = expander.io.ill  // illegal RVC 명령어

// F3Predecoder: brType/isCall/isRet 결정 (F2 predecode에서 불완전한 정보 보완)
f3Predecoder.in.instr = f3_instr
f3_pd(i).brType = f3Predecoder.out.pd(i).brType

// PredChecker: BPU 예측 vs 실제 decode 결과 비교
checkerIn = {ftqOffset, jumpOffset, target, instrRange, instrValid, pds, pc, fire_in}
checkerOutStage1 = 즉각 결과 (jal target mismatch 등)
checkerOutStage2 = 1-cycle 지연 결과 (더 복잡한 검사)

// wb_redirect: IFU가 스스로 FTQ에 redirect
wb_redirect = checkerOutStage1.valid (mispred 감지 시)

// MMIO 처리
if f3_pmp_mmio:
  MMIO 모드 진입 → toUncache.valid = true
  mmio_redirect = true → f2/f1 flush

// IBuffer 전송
io.toIbuffer.bits = {instrs, valid, enqEnable, pd, pc, exceptionType, ...}
io.toIbuffer.valid = f3_valid && !mmio_state

// FTQ writeback
toFtq.pdWb.valid = ...
toFtq.pdWb.bits = {pc, pd, ftqIdx, misOffset, cfiOffset, target, jalTarget, instrRange}
```

### 4.2 flush 전파

```
backend_redirect = fromFtq.redirect.valid
f3_flush = backend_redirect || (wb_redirect && !f3_wb_not_flush)
f2_flush = backend_redirect || mmio_redirect || wb_redirect
f1_flush = f2_flush
f0_flush = f1_flush || f0_flush_from_bpu  // BPU S2/S3 override 포함
```

### 4.3 내부 서브모듈

| 서브모듈 | 역할 |
| -------- | ---- |
| `PreDecode` | 16-bit half-word 스트림에서 명령어 경계 찾기, RVC 여부, 분기 타입 1차 판별 |
| `F3Predecoder` | F3에서 brType/isCall/isRet 최종 결정 |
| `PredChecker` | BPU 예측 vs 실제 predecode 비교, IFU redirect 생성 |
| `FrontendTrigger` | HW 트리거 (디버그) |
| `RVCExpander` × PredictWidth | 16-bit RVC → 32-bit 확장 |

---

## 5. Functionality

### 5.1 정상 fetch 흐름

1. FTQ가 `FetchRequestBundle`(startAddr, ftqIdx, ftqOffset) 제공
2. F0: ICache에 주소 전달 (FTQ → ICache 직접 연결), IFU는 req 소비
3. F1: PC 계산 (저전력 adder 분리)
4. F2: ICache 응답 수신 → 명령어 데이터 추출 → predecode → 예외 생성
5. F3: RVC 확장 → PredChecker → IBuffer 전송 + FTQ writeback

### 5.2 Cross-cacheline 처리

- `f0_doubleLine = startAddr(blockOffBits-1) === 1` → 두 번째 cacheline도 요청
- ICache가 두 cacheline 데이터를 5 bank × 8B 포맷으로 응답
- `f2_data_2_cacheline = Cat(data, data)` 로 2배 복제 후 `cut()` 함수로 추출

### 5.3 MMIO 명령어 처리

- F2에서 `f2_pmp_mmio` 감지 → F3에서 MMIO FSM 동작
- InstrUncache에 64-bit 단위 요청 → 응답에서 1개 명령어 추출
- ROB commit 확인(`mmioLastCommit`) 후 다음 MMIO fetch
- MMIO 중 backend redirect 가능 → 즉시 탈출

### 5.4 PredChecker 동작

- `jalFault`: JAL 명령어인데 BPU가 예측한 target이 다름
- `jalrFault`: JALR인데 target 불일치
- `retFault`: RET인데 target 불일치
- `targetFault`: taken 분기인데 target 불일치
- `notCFIFault`: CFI가 아닌 명령어가 taken으로 예측됨
- `invalidTakenFault`: fetch 범위 밖 명령어가 taken으로 예측됨
- 이상 감지 시 → `wb_redirect = true` → IFU가 FTQ에 수정 요청

---

## 6. Flow / Backpressure Control

| 조건 | 동작 |
| ---- | ---- |
| IBuffer full (`!toIbuffer.ready`) | f3_fire=0, f3_valid 유지, `icacheStop=!f3_ready` → ICache stall |
| ICache not ready (`!icacheReady`) | `fromFtq.req.ready=0` → FTQ stall |
| ICache miss (`!icacheRespAllValid`) | f2_fire=0, f2_valid 유지 → f1_ready=0 → FTQ stall |
| f2_icache_all_resp_reg | ICache 응답이 왔으나 F3 stall → reg에 저장, 재요청 방지 |
| Backend redirect | f0~f3 모두 flush, f1/f2/f3_valid=0 |
| BPU S2/S3 flush | f0_flush_from_bpu → f0 flush (F1 이후는 유지) |
| wb_redirect | f2/f3 flush (F1은 유지), FTQ에 수정 정보 전달 |
| MMIO redirect | f1/f2 flush, F3는 MMIO 완료까지 점유 |

---

## 7. Error / Exception Handling

| 예외 종류 | 감지 시점 | 처리 방법 |
| --------- | --------- | --------- |
| Page Fault (PF) | F2: `fromICache.bits.exception` (iTLB 결과) | `ExceptionType.pf` → FetchToIBuffer.exceptionType |
| Guest Page Fault (GPF) | F2: iTLB 응답 | `ExceptionType.gpf` → 동일 경로 |
| Access Fault (AF) | F2: PMP/ECC/TileLink corrupt | `ExceptionType.af` → 동일 경로 |
| MMIO mismatch | F2: double-line에서 pmp_mmio / itlb_pbmt 불일치 | AF 발생 (두 번째 라인에 표시) |
| Cross-page exception | F2: last-in-line RVI 명령어의 다음 page 예외 | `crossPage_exception_vec` → `IBufferExceptionType.CrossPF/GPF/AF` |
| Illegal RVC | F3: `expander.io.ill` | `IBufferExceptionType.rvcII` |

`ExceptionType.merge(iTLB_exception, mismatch_exception)` — 우선순위: iTLB > mismatch

---

## 8. Timing Hints

| Critical Path | 설명 |
| ------------- | ---- |
| F1: PC adder | `f1_pc_lower_result(i) = Cat(0,addr) + i*2` — PredictWidth개 병렬 adder. `CatPC` 최적화로 overflow 처리 분리 |
| F2: ICache addr match | `fromICache.bits.vaddr(0) === f2_ftq_req.startAddr` — timing critical 주석 (IFU.scala:365) |
| F2: cut() 함수 | Vec(blockBytes, 16-bit) indexing × (PredictWidth+1) — 큰 mux |
| F2: instr_range | `Fill(PW, ...) >> ~offset` — priority encoder 등가 |
| F3: PredChecker stage2 | 1-cycle 지연으로 타이밍 분리. checkerOutStage2에 복잡한 비교 |
| F3: toIbuffer | DecodeWidth개 valid masking + 예외 타입 인코딩 병렬 처리 |

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

- `f3_wb_not_flush`: PredChecker가 redirect를 감지했으나 F3의 결과는 IBuffer에 전달 완료 (non-flush 조건)
- IFU가 ICache에 stop 신호(`icacheStop`)를 보내는 것은 F3가 stall되어 F2가 막혔을 때 ICache 메인파이프도 정지시키기 위함
- `numOfStage = 3` 상수로 TopDown 스테이지 추적 배열 크기 결정
- FTQ-ICache 직접 연결: `icache.io.fetch.req <> ftq.io.toICache.req` (IFU가 중간에 없음), IFU는 응답만 수신
- `fetchQueueSize=2`: MMIO fetch 내부 버퍼 크기 (동시 최대 2개 pending 가능)
