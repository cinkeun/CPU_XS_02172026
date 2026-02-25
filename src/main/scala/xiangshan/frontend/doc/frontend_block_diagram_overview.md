# frontend_block_diagram_overview.md

- Block: Frontend
- Module: N/A (Top-level overview)
- Source: Frontend.scala, FrontendBundle.scala, bpu/Bpu.scala, ifu/Ifu.scala, ftq/Ftq.scala, ibuffer/IBuffer.scala
- Protocols: Decoupled / Valid / Credit
- Key Params: FtqSize, IBuffer.Size, NumWriteBank, NumReadBank, FetchBlockInstNum, DecodeWidth, PhrHistoryLength, GhrHistoryLength
- Last updated: 2026-02-18

---

## 1. Block Summary

XiangShan Frontend는 BPU(분기 예측) → FTQ(Fetch Target Queue) → IFU(명령어 패치) → IBuffer(명령어 버퍼) → Backend(디코드)로 이어지는 비순차적 명령어 공급 파이프라인이다.

- **주요 입력**: Backend로부터 오는 redirect (misprediction flush), sfence, tlbCsr, csrCtrl
- **주요 출력**: `io.backend.cfVec` (DecodeWidth개 CtrlFlow, IBuffer → Decode), `io.backend.stallReason` (stall 원인 정보)
- **성능/병목**: ICache miss (IFU stall), FTQ full (BPU back-pressure), IBuffer full (fetch stall), misprediction redirect flush

---

## 2. Key Parameters

| Parameter           | Source                                             | Default / 일반값 | 영향                                       |
| ------------------- | -------------------------------------------------- | --------------- | ------------------------------------------ |
| FtqSize             | `FtqParameters.FtqSize`                            | 64              | FTQ 엔트리 수 (BPU-IFU 버퍼 깊이)          |
| IBuffer.Size        | `IBufferParameters.Size`                           | 48              | IBuffer 총 엔트리 수                        |
| NumWriteBank        | `IBufferParameters.NumWriteBank`                   | 4               | IBuffer 쓰기 뱅크 수 (IFU pre-align용)     |
| NumReadBank         | `IBufferParameters.NumReadBank`                    | 8               | IBuffer 읽기 뱅크 수 (≥ DecodeWidth)        |
| FetchBlockInstNum   | `FetchBlockSize(64B) / instBytes`                  | 16 or 32        | 한 fetch 블록당 최대 명령어 수              |
| DecodeWidth         | `p(XSCoreParamsKey).DecodeWidth`                   | 6               | IBuffer → Decode 동시 출력 너비            |
| PhrHistoryLength    | `FrontendParameters.getPhrHistoryLength`           | 계산값           | PHR(Path History Register) 길이            |
| GhrHistoryLength    | `HasBpuParameters.GhrHistoryLength`                | SC 최대 table   | SC용 전역 분기 이력(GHR) 길이              |
| ResolveEntryBranchNumber | `FrontendParameters.ResolveEntryBranchNumber` | 8               | FTQ resolve 엔트리당 최대 분기 슬롯 수      |
| ipmpPortNum         | `coreParams.ipmpPortNum`                           | ICache ports    | PMP checker 포트 수                        |
| itlbPortNum         | `coreParams.itlbPortNum`                           | 1               | iTLB 포트 수                               |

---

## 3. Top-Level Block Diagram

```mermaid
---
config:
  layout: dagre
---
flowchart LR
 subgraph BPU["Predictor (BPU)"]
        BS3["S3\nMBTB+ITTAGE+RAS\ns3_override?"]
        BS2["S2\nMBTB+TAGE+SC\ns2_prediction"]
        BS1["S1\nUBTB+ABTB+UTAGE\n+MicroRAS\n1st prediction"]
        BS0["S0\nPC mux\nPHR/GHR gen"]
  end
 subgraph FTQ["Ftq"]
        FQ_COMM["commPtr\ncommit from ROB"]
        FQ_IFU["ifuPtr\ndeq to IFU"]
        FQ_BPU["bpuPtr\nenq from BPU"]
  end
 subgraph IFU["NewIFU"]
        F3["F3\nRVC expand\nPredChecker\nf3_valid RegInit"]
        F2["F2\nICache resp\nPredecode\nf2_valid RegInit"]
        F1["F1\nPC calc\nf1_valid RegInit"]
        F0["F0\nfetch req\nto ICache"]
  end
 subgraph ICache["ICache"]
        IC_PREFETCH["Prefetcher"]
        IC_MISS["MissHandler"]
        IC_MAIN["MainPipe\n(S0/S1/S2)"]
  end
 subgraph TLB_PMP["iTLB / PMP"]
        PMP["PMP\n+Checker"]
        ITLB["TLB\nPortNum+1 ports"]
  end
 subgraph IBuf["IBuffer"]
        IBOUT["Output Reg\nDecodeWidth"]
        IBK["IBufNBank 뱅크\n(Banked FIFO)"]
  end
    BS0 -- s0_fire RegEnable --> BS1
    BS1 -- s1_fire RegEnable --> BS2
    BS2 -- s2_fire RegEnable --> BS3
    FQ_BPU --> FQ_IFU
    FQ_IFU --> FQ_COMM
    F0 -- f0_fire RegEnable --> F1
    F1 -- f1_fire RegEnable --> F2
    F2 -- f2_fire ICache resp --> F3
    IC_MAIN --> IC_MISS
    IBK --> IBOUT
    CSR["CSR / sfence\n/ tlbCsr"] -. sfence/tlbCsr/csrCtrl .-> BPU
    CSR -. sfence/tlbCsr .-> TLB_PMP
    BS3 -- bpu_to_ftq Decoupled --> FTQ
    FTQ -- toBpu redirect/update Valid --> BPU
    FTQ -- toIfu Decoupled req --> IFU
    FTQ -- toICache Decoupled req --> ICache
    FTQ -- toPrefetch Decoupled --> IC_PREFETCH
    ICache -- "fetch.resp Valid" --> IFU
    ICache -- itlb --> ITLB
    ITLB -- ptw --> Backend["Backend\n(ROB/LSU)"]
    PMP -. pmp resp .-> ICache & IFU
    F3 -- toIbuffer Decoupled --> IBuf
    F3 -- pdWb Valid --> FTQ
    IBuf -- "cfVec + stallReason" --> Backend
    Backend -. "toFtq.redirect Valid" .-> FTQ
    Backend -. rob_commits .-> IFU
    FTQ -. icacheFlush .-> ICache
    FTQ -. flushFromBpu BpuFlushInfo .-> IFU
    IFU -. mmioCommitRead .-> FTQ
```

### 5.7 BPU 내부 s3_override

- S3 결과가 S1 예측과 다를 때 `s3_override = true` (`Bpu.scala:380`: `s3_valid && !(s3_prediction === s3_s1Prediction)`)
- override 발생 시 FTQ로 S3 예측 결과를 재전송 (`io.toFtq.prediction.bits.s3Override := s3_override`)
- S2 단계의 별도 override는 없으며, `s3_flush`가 발생하면 S2/S1도 함께 flush됨
- FTQ에서 IFU에게 `flushFromBpu` 전달 → IFU F0 단계에서 소비

---

## 6. Error / Exception Paths

### 6.1 iTLB 예외 (PF / GPF / AF)

- ICache → iTLB 요청 → `ExceptionType.fromTlbResp(resp)` → `fromICache.bits.exception` → IFU F2에서 수신
- `f2_exception_vec` 생성 → F3에서 `IBuffer.exceptionType`에 저장 → Decode까지 전달

### 6.2 PMP 접근 예외 (AF)

- `ExceptionType.fromPMPResp(resp)` → ICache 응답에 병합 → 동일 경로 전달

### 6.3 ECC / TileLink corrupt (AF)

- `ExceptionType.fromECC(enable, corrupt)` / `fromTilelink(corrupt)`
- ICache 내부에서 `io.error` Valid 출력 → `errorReg = RegNext(icache.io.error)` → `io.error` L1BusErrorUnit 전달 (`Frontend.scala:262-263`)

### 6.4 MMIO 명령어 처리

- IFU F3: `f3_pmp_mmio` 감지 → `InstrUncache` 경유 (64-bit 단위 fetch)
- MMIO flush: `mmio_redirect` → F2/F3 flush, FTQ에 snpc redirect
- ROB commit 확인 후 다음 MMIO fetch: `mmioCommitRead ↔ FTQ`

### 6.5 예외 우선순위 (ExceptionType.merge)

`iTLB(PF/GPF/AF) > PMP(AF) > ECC(AF)` — `ExceptionType.merge(...)` 함수 참조 (FrontendBundle.scala:191)

---

## 7. Timing Hints

| 모듈 | Critical Path 후보 | 설명 |
| ---- | ------------------ | ---- |
| IFU F1 | PC 계산 adder | `f1_pc_lower_result` 16개 adder 병렬 (`CatPC` 최적화 적용) |
| IFU F2 | ICache resp addr match | `fromICache.bits.vaddr(0) === f2_ftq_req.startAddr` — timing critical 주석 존재 (IFU.scala:366) |
| BPU S2/S3 | TAGE/SC multi-table lookup | 다수 folded history XOR + SRAM 읽기 |
| FTQ | FtqPtr 비교 | `isAfter` 함수 사용, 64-entry circular queue 비교 |
| IBuffer | enqOffset PopCount | `enqOffset = PopCount(io.in.bits.valid.take(i))` — PredictWidth(16) 너비 |
| BPU → IFU | `flushFromBpu.shouldFlushByStage2/3` | FtqPtr 비교로 flush 여부 판단, IFU F0에서 지연 없이 소비해야 함 |

---

## 8. Open Questions / TODO

- [ ] MMIO fetch latency: ROB commit wait이 얼마나 자주 발생하는가?
- [ ] `numDup=4` 복제 구조의 타이밍 절감 효과 검증
- [ ] `f2_mmio_mismatch_exception`: cacheable/non-cacheable 경계 crossing 케이스 처리 완전성
- [ ] PTWFilter의 `ifilterSize` 파라미터가 iTLB 미스 빈도에 미치는 영향
- [ ] IBuffer bypass 경로가 실제 waveform에서 얼마나 활용되는가?

---

→ See [frontend_in_out_seq_diagram.md](./frontend_in_out_seq_diagram.md)
→ See [frontend_Predictor_analysis.md](./frontend_Predictor_analysis.md)
→ See [frontend_NewIFU_analysis.md](./frontend_NewIFU_analysis.md)
→ See [frontend_Ftq_analysis.md](./frontend_Ftq_analysis.md)
→ See [frontend_IBuffer_analysis.md](./frontend_IBuffer_analysis.md)
