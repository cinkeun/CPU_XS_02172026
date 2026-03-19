# Frontend Performance Enhancement Suggestions

> 기반: code-based analysis (doc/*.md + Scala/Chisel 소스)
> 범위: 현재 구현 내에서 시도 가능한 방안만 포함 (파라미터 튜닝, 구조 변경, 로직 개선)
> 작성: 2026-03-18

---

## 목차

1. [BPU: TAGE 예측 커버리지 향상](#1-bpu-tage-예측-커버리지-향상)
2. [BPU: SC 임계값(Threshold) 세분화](#2-bpu-sc-임계값threshold-세분화)
3. [BPU: SC GlobalTable / BWTable 활성화](#3-bpu-sc-globaltable--bwtable-활성화)
4. [BPU: PHR pathHash 충돌 완화](#4-bpu-phr-pathhash-충돌-완화)
5. [BPU: S3 override 빈도 감소](#5-bpu-s3-override-빈도-감소)
6. [BPU: mBTB 용량 및 뱅킹 개선](#6-bpu-mbtb-용량-및-뱅킹-개선)
7. [ICache: 프리페치 적중률 향상](#7-icache-프리페치-적중률-향상)
8. [ICache: 미스 페널티 감소 (MSHR 수 조정)](#8-icache-미스-페널티-감소-mshr-수-조정)
9. [ICache: WayLookup 큐 깊이 조정](#9-icache-waylookup-큐-깊이-조정)
10. [FTQ: 큐 사이즈 및 바이패스 최적화](#10-ftq-큐-사이즈-및-바이패스-최적화)
11. [IFU: 크로스-캐시라인 패치 파이프라이닝](#11-ifu-크로스-캐시라인-패치-파이프라이닝)
12. [IBuffer: 사이즈 및 뱅크 수 튜닝](#12-ibuffer-사이즈-및-뱅크-수-튜닝)

**추가 섹션 (심화 연구 방안)**

13. [새로운 BTB/PRED 방식 제안 — misprediction 근본 감소](#13-새로운-btbpred-방식-제안--misprediction-근본-감소)
14. [기존 BTB/PRED 미스율·오예측률 감소 방안](#14-기존-btbpred-미스율오예측률-감소-방안)
15. [ICache: BE pressure 연동 동적 프리페치](#15-icache-be-pressure-연동-동적-프리페치)
16. [Predecoder: Instruction Fusion 및 신규 Functional Unit 제안](#16-predecoder-instruction-fusion-및-신규-functional-unit-제안)

---

## 1. BPU: TAGE 예측 커버리지 향상

### 문제

TAGE는 fetch block 당 **최대 2개 branch**만 직접 커버할 수 있다. 이는 SRAM이 한 번에 1개 bank × 1개 setIdx를 읽고, 결과로 `NumWays=2`개 entry만 반환하기 때문이다. 3번째 이상의 branch는 구조적으로 TAGE miss가 보장되어 mBTB의 2-bit saturating counter로 fallback된다.

```scala
// tage/Parameters.scala:48
new TageTableInfo(4096, 2, 4),   // NumWays=2 고정
// tage/TageTable.scala:49-73
// entrySram: NumBanks(4) × NumWays(2) = 8 SRAM 인스턴스
```

```scala
// tage/Tage.scala:523-525
// 3번째+ branch는 rawTag ^ position으로 동일 2개 entry를 tag 비교하지만
// NumWays=2이므로 구조적 miss
```

### 방안

**A. NumWays 증가 (2 → 4)**
- `TageTableInfo(4096, 4, histLen)` 로 변경
- 각 bank의 SRAM 인스턴스: 8개 → 16개
- fetch block 내 4개 branch까지 직접 TAGE 커버 가능
- **비용**: SRAM 면적 2배, 타이밍 압박 증가

**B. TableInfo Size 증가 (4096 → 8192)**
- set 수 증가: 512 → 1024
- aliasing 감소 효과
- `SetIdxWidth` 9→10 bits 자동 확장됨
- **비용**: SRAM 면적 2배, tag fold width가 일부 테이블에서 달라짐

**C. 단기 히스토리 테이블 전용 NumWays 확장**
- Table 0 (histLen=4) ~ Table 2 (histLen=17) 만 NumWays 증가
- 단기 히스토리는 포화(saturation)가 빠르므로 더 많은 entry가 유효
- 장기 히스토리 테이블은 현행 유지

### 관련 파일

- [bpu/tage/Parameters.scala](../bpu/tage/Parameters.scala)
- [bpu/tage/TageTable.scala](../bpu/tage/TageTable.scala)

---

## 2. BPU: SC 임계값(Threshold) 세분화

### 문제

현재 SC threshold는 **8개 레지스터 (per way-slot)** 로만 세분화되어 있다. 같은 way-slot을 공유하는 서로 다른 PC들이 threshold를 오염(pollute)할 수 있다. Hard-to-predict branch가 threshold를 높이면, 같은 slot의 easy-to-correct branch도 동일하게 높은 threshold를 받아 SC 개입 기회를 잃는다.

```scala
// sc/Sc.scala:78
val scThreshold = RegInit(VecInit.tabulate(NumWays)(_ => ThresholdCounter.Init))
// NumWays=8, threshold는 per-way-slot 레지스터 — PC/tag 차원 없음
val thres = s2_thresholds(s2_wayIdx(i))  // wayIdx만으로 인덱싱
```

### 방안

**A. ThresholdInit 값 튜닝**
- 현재: `ThresholdInit = 720`
- 효과적 threshold: High=45, Mid=22, Low=11 (value >> 4/5/6)
- workload에 따라 threshold가 너무 보수적(높음)이거나 공격적(낮음)일 수 있음
- 시뮬레이션으로 최적값 탐색: 480~960 범위

**B. Per-set threshold (SRAM 기반)**
- 현재 8개 레지스터를 128-set SRAM으로 교체
- set index: `PC[high] % 128`
- PC별 독립 threshold 학습 가능
- **비용**: SRAM 추가, 1-cycle 읽기 지연 발생 (s1에서 읽어 s2 사용)

**C. ThresholdWidth 조정 (12 → 10 or 14)**
- 현재: 12-bit (범위 0-4095)
- 넓힐수록 threshold 안정성 증가, 줄일수록 적응 속도 증가
- 훈련 부하 없이 간단히 파라미터 변경으로 시도 가능

**D. tageConf 구간 분할 조정 (>> 1/2/3 → 다른 비율)**
```scala
// sc/Sc.scala:318-334
// tageConfHigh → thres >> 1 (50% threshold)
// tageConfMid  → thres >> 2 (25% threshold)
// tageConfLow  → thres >> 3 (12.5% threshold)
```
- 비율을 `>>2/>>3/>>4` 등으로 조정하면 SC의 전반적 개입 빈도 변화

### 관련 파일

- [bpu/sc/Parameters.scala](../bpu/sc/Parameters.scala)
- [bpu/sc/Sc.scala](../bpu/sc/Sc.scala)

---

## 3. BPU: SC GlobalTable / BWTable 활성화

### 문제

현재 default config에서 `GlobalEnable=false`, `BWEnable=false`로 GlobalTable과 BWTable이 비활성화되어 있다. GlobalTable은 GHR 기반 분기 패턴, BWTable은 루프/후방 분기 패턴을 학습하는데, 이들이 비활성화된 상태에서는 PathTable + BiasTable만으로 SC correction이 이뤄진다.

```scala
// sc/Parameters.scala:28-30
GlobalEnable: Boolean = false,  // disabled by default
BWEnable:     Boolean = false,  // disabled by default
```

### 방안

**A. GlobalEnable = true 시도**
- GlobalTable: `(128, hist=8)` + `(128, hist=16)` 2개 테이블 활성화
- 단기 GHR(≤16) 기반 분기 상관관계 추가 학습
- `sc.io.commonHR`을 통해 GHR이 이미 공급되고 있으므로 배선 변경 없음
- **비용**: SRAM 면적 증가 (PathTable과 동일 크기 2개 추가), percsum 계산 path 추가

**B. BWEnable = true 시도**
- BWTable: `(128, hist=4)` + `(128, hist=8)` 2개 테이블 활성화
- 루프 exit branch, 중첩 루프 패턴 학습
- **비용**: PathTable 면적의 절반 (hist=4/8, 더 짧은 fold width)

**C. 선택적 활성화 (workload별)**
- 정수 workload: GlobalEnable=true 우선 (제어 흐름 상관관계 많음)
- 과학 계산 workload: BWEnable=true 우선 (루프 패턴 많음)

**주의**: 현재 코드에서 `commonHR.valid` 체크 로직이 있어, 유효하지 않은 GHR 상태에서 GlobalTable/BWTable이 0으로 처리됨. 활성화 시 commonHR 초기화/학습 지연이 효과에 영향을 줄 수 있다.

### 관련 파일

- [bpu/sc/Parameters.scala](../bpu/sc/Parameters.scala)
- [bpu/sc/Sc.scala](../bpu/sc/Sc.scala) (percsum 계산: 235-240)

---

## 4. BPU: PHR pathHash 충돌 완화

### 문제

PHR의 `pathHash`는 `pc[9:1]`과 `target[16:2]`의 15-bit XOR에 기반한다. 이 해시가 충돌(aliasing)하면, 서로 다른 실행 경로가 동일한 PHR 시퀀스를 생성하여 TAGE의 동일한 entry를 공유하게 된다.

```scala
// bpu/history/phr/Helpers.scala:60-63
def pathHash(pc: PrunedAddr, target: PrunedAddr): UInt = {
  val hash = Cat(pc(9, 1), 0.U(4.W)) ^ target(16, 2)
  hash(PathHashWidth - 1, 0)  // 15-bit only
}
```

충돌 시나리오 (sc_analysis.md의 예시):
- Loop A (pc=0x8000_0100) → PathHash XOR folding = 0x3A
- Loop B (pc=0x8000_0500) → PathHash XOR folding = 0x3A
→ 동일 TAGE entry 공유 → TAGE weakTaken 오염

### 방안

**A. PathHashWidth 증가 (15 → 18 or 20)**
```scala
// bpu/history/phr/Parameters.scala
PathHashWidth = 15  →  18
```
- 해시 공간 확장: 32768 → 262144 가지
- aliasing 확률 지수적 감소
- **비용**: Shamt=2 유지 시 PHR 총 길이 변경 없음 (hashHigh 비트 수만 증가), fold 계산 논리 변경

**B. Shamt 증가 (2 → 3)**
```scala
// bpu/history/phr/Parameters.scala
Shamt = 2  →  3
```
- taken branch당 PHR에 입력되는 비트 수 증가
- 더 빠른 경로 정보 갱신
- **비용**: PHR 총 길이 재계산 필요: `397 + 3×64 + 4 = 593 → 596` bits
  (`Shamt*FtqSize`가 128→192로 증가)

**C. pathHash 함수 개선**
- 현재: `Cat(pc(9,1), 0.U(4.W)) ^ target(16,2)` — PC의 중간 비트 + target의 중간 비트
- 대안: PC의 더 높은 비트 포함 (library/OS 로딩 주소 차이 반영)
  ```scala
  // 예: pc[17:9] 포함하여 더 넓은 범위 커버
  val hash = Cat(pc(17, 9), 0.U(4.W)) ^ target(20, 6)
  ```
- **주의**: 이 변경은 모든 PHR consumer (TAGE, uTAGE, ITTAGE, SC)의 fold 일관성에 영향

### 관련 파일

- [bpu/history/phr/Parameters.scala](../bpu/history/phr/Parameters.scala)
- [bpu/history/phr/Helpers.scala](../bpu/history/phr/Helpers.scala)
- [bpu/history/phr/Phr.scala](../bpu/history/phr/Phr.scala)

---

## 5. BPU: S3 override 빈도 감소

### 문제

S3 override는 S3 예측이 S1 예측과 다를 때 발생한다. Override 시 `bpuPtr` 롤백 + IFU flush가 발생하여 파이프라인 낭비가 생긴다. override가 자주 발생한다는 것은 S1(FauFTB)이 S3(TAGE+SC+ITTAGE+RAS)와 자주 다른 예측을 한다는 의미다.

```scala
// bpu/Bpu.scala
s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)
// s3_override는 FTQ ready 여부와 무관하게 발생 (unconditional)
```

### 방안

**A. uTAGE (S1 방향 예측기) 용량 증가**
```scala
// bpu/fauftb/Parameters.scala
UTageTableInfos = [(512, 2, 9), (512, 2, 16)]
//                  ↑ Size 증가 검토: 512 → 1024
```
- uTAGE는 FF-based (SRAM 아님)이므로 면적 비용이 큼
- S1 예측의 방향 정확도 향상 → S3 override 감소
- **대안**: uTAGE는 그대로 두고, S2에서 FTQ에 prediction 전달하는 구조 검토 (현재 S2는 FTQ에 직접 전달하지 않음)

**B. uBTB (FauFTB) entry 수 증가**
```scala
// bpu/fauftb/Parameters.scala
UbtbSize = 128  →  256
```
- S1 target 정확도 향상 → S3 override에서 target 불일치 원인 감소
- FF-based이므로 크기 증가에 면적 민감

**C. S3 override를 S2 결과로 조기 탐지**
- 현재: S3에서만 override 발생
- 개선 아이디어: S2에서 이미 mBTB+TAGE 결과가 나오면, S2에서 S1과 비교하여 조기 override
- **장점**: override를 1 cycle 앞당겨 파이프라인 플러시 피해 감소
- **단점**: FTQ로의 S2 path 추가 필요 (현재 S2는 FTQ write 경로 없음)

### 관련 파일

- [bpu/fauftb/Parameters.scala](../bpu/fauftb/Parameters.scala) (존재 여부 확인 필요)
- [bpu/Bpu.scala](../bpu/Bpu.scala) (s3_override 로직)

---

## 6. BPU: mBTB 용량 및 뱅킹 개선

### 문제

mBTB는 8192 entry, 2 alignBank × 4 internalBank × 4 way 구조다. region 단위(32B)로 인덱싱하므로, 같은 32B 내의 branch들이 모두 동일한 set으로 매핑된다. fetch block(64B)에 걸쳐있는 branch들은 2개 alignBank로 분산된다.

```scala
// mbtb/Parameters.scala
NumSets:        Int = 8192 / (NumAlignBanks * NumInternalBanks * NumWay)
              = 8192 / (2 * 4 * 4) = 256 (per internal bank)
```

### 방안

**A. NumWay 증가 (4 → 8)**
- 동일 region에서 더 많은 branch를 캐시 가능
- SRAM 너비만 증가 (depth 변화 없음)
- 단, way 선택 로직과 TAGE의 NumBtbResultEntries 의존성 확인 필요
- `NumBtbResultEntries = NumWays × NumAlignBanks = 4×2=8 → 8×2=16`으로 증가
  → TAGE, SC, FTQ 전체 bundleWidth 영향 (대규모 변경)

**B. 전체 entry 수 증가 (8192 → 16384)**
- 각 bank의 NumSets: 256 → 512
- SRAM 깊이 2배
- 인덱스 비트 1개 추가
- BTB 미스로 인한 fallthrough 예측 감소
- **비용**: SRAM 면적 2배

**C. WriteBuffer size 증가 (4 → 8)**
```scala
// mbtb/Parameters.scala
WriteBufferSize: Int = 4  →  8
```
- 훈련 업데이트와 예측 읽기의 충돌 빈도 감소
- 특히 tight loop에서 훈련 업데이트가 연속 발생할 때 stall 감소
- 비용: 작음 (레지스터 기반)

### 관련 파일

- [bpu/mbtb/Parameters.scala](../bpu/mbtb/Parameters.scala)

---

## 7. ICache: 프리페치 적중률 향상

### 문제

ICache의 하드웨어 프리페치는 FTQ가 예측한 주소를 앞당겨 로딩한다. 프리페치가 실제 fetch 요청보다 충분히 앞서지 못하면 여전히 main pipe 스톨이 발생한다. 또한 WayLookup 큐(32 entry)가 가득 차면 prefetch pipe가 정체된다.

```
// icache/ICacheMissUnit.scala
NumFetchMshr = 4       // 실제 fetch용
NumPrefetchMshr = 10   // 프리페치용
```

### 방안

**A. NumPrefetchMshr 증가 (10 → 14)**
- 동시에 더 많은 미스를 in-flight으로 처리 가능
- prefetch가 fetch보다 훨씬 앞서있는 경우(긴 루프) 효과적
- **비용**: MSHR state machine 인스턴스 추가 (논리 회로, SRAM 없음)

**B. WayLookup 큐 깊이 증가 (32 → 48)**
```scala
// icache/Parameters.scala
WayLookupSize = 32  →  48
```
- prefetch가 fetch보다 많이 앞서있을 때 WayLookup stall 방지
- ICache stall counter (`wayLookupStallCnt`)로 효과 측정 가능

**C. 프리페치 aggressiveness 조정**
- 현재 프리페치는 FTQ가 제공하는 주소 기반
- BPU가 예측한 next target에 더 빠르게 반응하도록 FTQ→ICache prefetch 경로의 파이프라인 단축
- FTQ의 `s3FtqPtr`이 쓰인 직후 prefetch 요청 트리거 검토

**D. NumFetchMshr 증가 (4 → 6)**
- sequential memory access가 많은 workload에서 fetch MSHR이 bottleneck이 될 수 있음
- main pipe stall counter (`mainPipeStallCnt`)로 효과 측정 가능

### 관련 파일

- [icache/Parameters.scala](../icache/Parameters.scala)
- [icache/ICacheMissUnit.scala](../icache/ICacheMissUnit.scala)
- [icache/ICacheWayLookup.scala](../icache/ICacheWayLookup.scala)

---

## 8. ICache: 미스 페널티 감소 (MSHR 수 조정)

### 문제

ICache miss 시 L2까지 접근해야 하므로 수십 cycle의 penalty가 발생한다. Miss가 발생하는 동안 IFU F2가 stall되고, 이 stall이 F0까지 역전파되어 BPU → FTQ 전체가 정체된다.

### 방안

**A. MSHR merge 정책 개선**
- 현재: 동일 주소 중복 제거만 수행
- 개선: 주소가 다르지만 **같은 L2 캐시라인**에 속하는 요청을 merged pending으로 처리
  (`blkPAddr[63:6]`이 같을 경우 동일 L2 cacheline)
- L2 접근 횟수 감소 → 전체 miss penalty 감소

**B. Prefetch와 Fetch MSHR 사이의 동적 파티셔닝**
- 현재: fetch 4 / prefetch 10 고정 분할
- 개선: 총 14개 MSHR을 demand-based로 할당 (fetch 요청 급증 시 prefetch MSHR 일부를 fetch로 전환)
- 구현: `NumFetchMshr + NumPrefetchMshr = 14` 유지하면서 우선순위 큐 방식 도입

**C. Critical-word-first 지원 여부 확인**
- 현재 MSHR은 L2의 모든 beat를 받은 후 DataArray에 기록
- 만약 IFU가 필요한 특정 은행의 데이터가 먼저 도착하면 조기 응답 가능
- 코드 상에서 `refillCycles=2` beat 중 1st beat에서 조기 응답 로직이 있는지 확인 후 없으면 추가 검토

### 관련 파일

- [icache/ICacheMshr.scala](../icache/ICacheMshr.scala)
- [icache/ICacheMissUnit.scala](../icache/ICacheMissUnit.scala)

---

## 9. ICache: WayLookup 큐 깊이 조정

### 문제

WayLookup은 prefetch pipe와 main pipe 사이의 ring queue(32 entry)다. prefetch가 fetch보다 32 entry 이상 앞서가면 WayLookup이 가득 차서 prefetch가 멈춘다. stall counter `wayLookupStallCnt`로 이 현상을 관찰할 수 있다.

```scala
// icache/Parameters.scala
WayLookupSize = 32
```

### 방안

**A. WayLookupSize 증가 (32 → 48 or 64)**
- prefetch와 fetch 사이의 거리가 늘어날수록 효과 증가
- WayLookup entry는 포트당 `wayMask + pTag + metaCodes + vSetIdx + ptlbExcp`를 저장하므로 entry당 수백 비트
- 면적 증가는 있으나 SRAM이 아닌 레지스터 기반이므로 timing impact 낮음

**B. Bypass 조건 확인 및 최적화**
```scala
// icache/ICacheWayLookup.scala
// bypass: queue empty && prefetcher writing && main pipe reading
```
- bypass가 활성화되면 큐를 거치지 않아 1-cycle 절약
- bypass 조건이 너무 restrictive하지 않은지 코드 레벨 확인

### 관련 파일

- [icache/ICacheWayLookup.scala](../icache/ICacheWayLookup.scala)
- [icache/Parameters.scala](../icache/Parameters.scala)

---

## 10. FTQ: 큐 사이즈 및 바이패스 최적화

### 문제

FTQ는 64 entry circular queue로, BPU와 IFU 사이의 버퍼 역할을 한다. FTQ full 시 BPU S0/S1이 stall되어 새로운 예측을 내보내지 못한다. ICache miss가 길어지면 IFU가 FTQ entry를 소비하지 못해 FTQ full 상태가 지속된다.

```scala
// ftq/FtqParameters.scala
FtqSize = 64
```

### 방안

**A. FtqSize 증가 (64 → 96 or 128)**
- BPU가 ICache miss 동안에도 예측 계속 가능
- 미스 복귀 후 IFU에 즉시 공급할 entry 확보
- **비용**: FTQ 내 5종류의 메모리(PC, redirect, meta, PD, FTB) 모두 entry 수 증가
  - meta 저장소는 `MaxMetaLength=512 bits × FtqSize`이므로 면적 가장 큼
  - 64→128 시 meta 메모리 2배 증가

**B. BPU → FTQ 바이패스 경로 유지**
- 현재: BPU enqueue entry가 `ifuPtr`과 같으면 IFU로 0-cycle bypass
- 이 경로가 stall 없이 동작하는지 timing 확인
- bypass가 막히는 조건 (FTQ full 외)이 있는지 검토

**C. S3 override 발생 시 FTQ entry 재사용**
- 현재: S3 override → `bpuPtr` 롤백 → 이전 entry들이 덮어씌워짐
- S3 override로 롤백된 entry들 중 실제로 flush되어야 하는 entry 수 최소화 검토
- (구조적 제약이 크므로 feasibility 확인 필요)

### 관련 파일

- [ftq/FtqParameters.scala](../ftq/FtqParameters.scala) (존재 여부 확인)
- [ftq/Ftq.scala](../ftq/Ftq.scala)

---

## 11. IFU: 크로스-캐시라인 패치 파이프라이닝

### 문제

IFU가 캐시라인 경계를 넘는 fetch (doubleLine=true)를 처리할 때, ICache에 2개 포트로 동시에 요청하지만, 두 라인 모두 ready가 되어야 F2가 진행된다. 한 라인이 캐시 히트, 다른 라인이 미스인 경우 히트 라인의 데이터를 기다리게 된다.

```scala
// ifu/IFU.scala
// f2_fire = f2_valid && f3_ready && icacheRespAllValid
// icacheRespAllValid: 두 포트 모두 유효해야 함
```

### 방안

**A. 부분 결과로 partial predecode 수행**
- 첫 번째 캐시라인(hit)이 도착하면 그 범위 내의 instruction들을 선제적으로 predecode
- 두 번째 라인(miss) 도착 후 나머지를 처리
- RVC expansion은 두 라인 합쳐야 정확하지만, 첫 라인 내 완전한 instruction들은 선처리 가능

**B. doubleLine 발생 빈도 최적화**
- fetch 시작 주소 정렬 (항상 64B 경계에서 시작하도록 BPU 예측 보정)
- 현재 BPU는 arbitrary PC를 시작점으로 사용하므로 어려울 수 있으나, 64B align 조건 시 doubleLine 0%

**C. ICache 2포트 데이터 독립 전달**
- 현재 구조에서 IFU F2가 두 포트 모두 기다리는 대신, 각 포트 데이터를 독립 레지스터에 저장하고 두 번째 도착 시 조합하는 방식 검토
- F2 stall 사이클을 줄일 수 있으나 F2/F3 로직 복잡도 증가

### 관련 파일

- [ifu/IFU.scala](../ifu/IFU.scala)
- [icache/ICacheMainPipe.scala](../icache/ICacheMainPipe.scala)

---

## 12. IBuffer: 사이즈 및 뱅크 수 튜닝

### 문제

IBuffer(48 entry, 4 write banks, 8 read banks)는 IFU와 Decode 사이의 버퍼다. IBuffer가 가득 차면 IFU F3가 stall된다. 이 stall은 역전파되어 F2, F1, F0까지 전파된다. Backend 처리 속도(DecodeWidth=6)보다 IFU 공급 속도(최대 PredictWidth=16)가 높으므로 순간적 burst에서 full이 발생할 수 있다.

```scala
// ibuffer/IBuffer.scala
IBufferSize = 48
NumWriteBank = 4   // IFU가 한 번에 쓰는 뱅크 수
NumReadBank  = 8   // Decode가 읽는 뱅크 수
DecodeWidth  = 6   // backend 소비 속도
```

### 방안

**A. IBufferSize 증가 (48 → 64)**
- ICache miss로 인한 IFU burst와 Decode 사이의 버퍼링 여유 증가
- full 빈도 감소 → IFU F3 stall 감소
- **비용**: 레지스터 기반이므로 면적은 선형 증가, timing 영향 적음

**B. NumWriteBank 증가 (4 → 8)**
- 현재 IFU가 한 번에 PredictWidth=16 instruction을 써야 하지만, 4 write bank로만 처리
- 뱅크 수를 늘리면 write side가 더 균일하게 분산
- 단, write bank alignment 로직도 함께 변경 필요

**C. IBuffer prefill 정책 검토**
- IBuffer가 비어있을 때 IFU가 더 적극적으로 fill하도록 백프레셔 신호 조정
- 현재 `toIbuffer.ready` 기반의 단순 handshake에서, `numValid < threshold` 조건으로 선제적 요청 증가 가능

### 관련 파일

- [ibuffer/IBuffer.scala](../ibuffer/IBuffer.scala)

---

## 우선순위 요약

| 방안 | 기대 효과 | 구현 난이도 | 비용 |
|---|---|---|---|
| 3. SC GlobalTable 활성화 | 중-상 | 낮음 (파라미터 변경) | SRAM 면적 소폭 증가 |
| 7B. WayLookup 큐 증가 | 중 | 낮음 (파라미터 변경) | 레지스터 소폭 증가 |
| 12A. IBufferSize 증가 | 중 | 낮음 (파라미터 변경) | 레지스터 면적 증가 |
| 10A. FtqSize 증가 | 중 | 낮음 (파라미터 변경) | meta 메모리 큰 증가 |
| 6C. mBTB WriteBuffer 증가 | 낮-중 | 낮음 (파라미터 변경) | 레지스터 소폭 증가 |
| 2A. ThresholdInit 튜닝 | 중 | 낮음 (파라미터 변경) | 없음 |
| 1B. TAGE Size 증가 | 중-상 | 중간 | SRAM 면적 2배 |
| 4A. PathHashWidth 증가 | 중 | 중간 | fold 로직 변경 |
| 7A. PrefetchMshr 증가 | 중 | 중간 | 회로 증가 |
| 2B. Per-set threshold | 중-상 | 높음 | SRAM 추가 + timing |
| 1A. TAGE NumWays 증가 | 상 | 높음 | 대규모 bundle 변경 |
| 5C. S2 early override | 중 | 높음 | FTQ path 추가 |

> 낮은 난이도 방안부터 시도하여 시뮬레이션으로 효과를 측정한 뒤, 효과가 검증된 방안을 선택적으로 상위 방안으로 확장하는 것을 권장.

---

*분석 기반: doc/*.md (sc_analysis, tage_analysis, icache_analysis, phr_analysis, mbtb_analysis 외) + Scala 소스 코드*

---

---

## 13. 새로운 BTB/PRED 방식 제안 — misprediction 근본 감소

> 현재 구조(uBTB→mBTB+TAGE+SC+ITTAGE+RAS)의 구조적 빈 틈을 메우는 새로운 예측기 아이디어를 정리한다.
> 각 방안은 기존 파이프라인의 S1/S2/S3 슬롯 중 어디에 삽입 가능한지도 함께 명시한다.

---

### 13.1 Loop Predictor (전용 루프 카운터 예측기)

#### 동기

현재 SC의 BWTable(`BackwardTableInfos`)이 설계는 되어 있으나 **`BWEnable=false`** 로 비활성화되어 있다. 루프 branch는 반복 횟수가 고정되면 TAGE-SC로도 처리 가능하지만, **가변 루프 (런타임 횟수 결정)** 의 마지막 iteration에서 반드시 오예측이 1회 발생한다. TAGE는 long history를 봐야 하므로 cold start 문제도 있다.

```
// 루프 패턴 예:
// for (int i = 0; i < N; i++) { ... }
// beq x1, x2, exit  ← N번 not-taken, 마지막 1번 taken
// TAGE는 history에서 taken이 보이기 전까지 not-taken으로 saturate → exit에서 오예측 필수
```

#### 제안: 전용 Loop Predictor (S2 슬롯)

```
구조:
  LoopEntry {
    tag:       PC[high] (tag 비교)
    tripCount: UInt(10.W)   // 최대 1023 반복
    counter:   UInt(10.W)   // 현재 카운터
    confidence: UInt(2.W)   // 학습 완료 여부
  }
  크기: 64 ~ 128 entry (직접 매핑 or 2-way)

동작:
  1. PC 히트 + confidence 높음 → taken은 counter < tripCount, not-taken은 counter == tripCount
  2. 백엔드 커밋 시 실제 반복 횟수로 tripCount 업데이트
  3. TAGE와 충돌 시: confidence가 높은 Loop Predictor 우선
```

**삽입 위치**: S2 (mBTB와 동시에 SRAM 읽기, SC와 함께 direction override)

**기대 효과**:
- 가변 루프의 마지막 iteration 오예측 제거
- 특히 Spectre-v1 방어 코드, 소팅 알고리즘, 문자열 처리 등에서 두드러진 효과

**관련 파일**: [bpu/Bpu.scala](../bpu/Bpu.scala), [bpu/sc/Parameters.scala](../bpu/sc/Parameters.scala) (BWEnable과 동일 레이어에 추가)

---

### 13.2 Perceptron Predictor (퍼셉트론 예측기) — TAGE 보완

#### 동기

TAGE는 단일 provider (가장 긴 히스토리 히트)의 counter sign을 최종 예측으로 사용한다. SC가 보정하지만 SC도 miss하는 패턴이 존재한다: **여러 히스토리 길이의 정보를 동시에 weighted sum해야 정확한 경우**다. 퍼셉트론은 이를 자연스럽게 처리한다.

```
Perceptron 예측 원리:
  y = w0 + Σ(wi × h_i)   (h_i = GHR의 i번째 비트, +1/-1)
  y >= 0 → taken, y < 0 → not-taken
  학습: y 부호가 실제와 다르거나 |y| < threshold → wi += actual_direction
```

#### 제안: Hashed Perceptron (S2/S3 슬롯)

```
구조:
  테이블: 512 entries × 8 가중치 (각 8비트 signed)
  인덱스: PC[low] XOR fold(GHR, log2(512))
  가중치 선택: GHR의 8개 구간별 각 1개 가중치

  예측:
    sum = Σ(weight_table[idx][i] × Mux(ghr[i*33], +1, -1)) + bias
    taken = sum >= 0
```

**현재 구조와의 통합 방법**:
- 독립적으로 예측 생성 후, TAGE의 `useAltOnNa` 와 유사하게 **TAGE provider가 weak일 때만 perceptron 결과 사용**
- SC의 역할(threshold-based override)을 퍼셉트론으로 부분 교체하거나 추가 vote로 사용

**주의사항**:
- 가중치 합산(sum 계산)에 adder tree 필요 → timing critical
- S3에 배치하면 `s3_fire`에서 combinatorial path 추가
- GHR을 사용하므로 commonHR이 활성화되어 있어야 함

**관련 파일**: [bpu/sc/Sc.scala](../bpu/sc/Sc.scala) (SC 구조와 유사하게 추가 가능)

---

### 13.3 Indirect Branch Predictor 강화 — ITTAGE 확장

#### 동기

현재 ITTAGE는 **2개 테이블, 각 1024 entry, histLen 4/16** 으로 매우 작다. 가상 함수 호출, 함수 포인터, `switch-case` (jump table), `longjmp` 등 간접 분기는 target이 런타임에 결정되므로 오예측 시 전체 파이프라인 flush가 발생하고 페널티가 크다.

```scala
// bpu/ittage/Parameters.scala
ITTageTableInfos = Seq(
  new ITTageTableInfo(1024, 4),   // histLen=4
  new ITTageTableInfo(1024, 16),  // histLen=16
)
// NumWays = 2 (per bank)
```

#### 제안: ITTAGE 테이블 수 증가 + histLen 확장

```scala
// 안 A: 테이블 수 4개로 증가
ITTageTableInfos = Seq(
  new ITTageTableInfo(1024, 4),
  new ITTageTableInfo(1024, 9),
  new ITTageTableInfo(1024, 16),
  new ITTageTableInfo(1024, 32),
)

// 안 B: 테이블 크기 증가
ITTageTableInfos = Seq(
  new ITTageTableInfo(2048, 4),
  new ITTageTableInfo(2048, 16),
)
```

**기대 효과**:
- C++ 가상 함수 벤치마크에서 간접 분기 오예측률 감소
- histLen=32 추가로 더 긴 context 패턴 캡처 (OOP 코드의 다형성 호출 패턴)

**추가 제안: ITTAGE + 호출 인수 해시**
- 현재 ITTAGE는 PC와 PHR만 사용
- 간접 점프 대상이 함수 인수에 종속되는 경우 (예: `call *table[type]`) type 레지스터 값의 하위 비트를 인덱스에 추가
- **구현 위치**: [bpu/ittage/Ittage.scala](../bpu/ittage/Ittage.scala) (인덱스 계산 부분)

---

### 13.4 BTB 미스 시 Fallthrough 예측 개선 — FTB Ghost Entry 제거

#### 동기

FTQ의 `entry_hit_status`에는 `h_false_hit` 상태가 있다. FTB에 ghost entry(실제 branch가 없는데 branch로 기록된 entry)가 존재하면, 해당 블록이 fetch될 때마다 잘못된 target으로 예측되어 IFU PredChecker에서 항상 오예측이 발생한다. Ghost entry는 self-modifying code, JIT 코드, OS 커널 패치 등에서 발생할 수 있다.

```scala
// ftq/NewFtq.scala
// has_false_hit 검출 → entry_hit_status := h_false_hit
// BPU update에 false_hit 플래그 전달 → FTB entry 무효화
```

#### 제안: Ghost Entry 조기 무효화 메커니즘

```
현재: false_hit 검출 → IFU writeback → FTQ commit → BPU update → FTB 무효화
      (최소 수십 cycle 지연)

개선안: false_hit 검출 즉시 FTB의 valid bit 무효화 (IFU pdWb 경로로 직접)
  → FTB entry 무효화를 IFU writeback 시점에 수행
  → 같은 PC를 다시 fetch하더라도 ghost entry로 인한 오예측 없음
```

**추가**: FTB entry 학습 시 2-cycle 확인 메커니즘 도입
- 처음 branch가 보였을 때 바로 FTB에 기록하지 않고, 2번 연속 확인 후 기록
- Ghost entry 생성 빈도 감소

**관련 파일**: [ftq/Ftq.scala](../ftq/Ftq.scala) (false_hit 처리), FTB 학습 로직

---

### 13.5 Confidence-based Prediction Skipping (S3 생략)

#### 동기

S3 예측기(TAGE full + SC + ITTAGE + RAS)는 매 cycle 동작하지만, 결과가 S1과 동일하면 override가 발생하지 않아 낭비다. S3 결과를 기다리는 것 자체가 3 cycle의 예측 latency를 의미한다. 만약 S1 예측의 신뢰도가 매우 높다면, S3를 미리 확정(commit)하고 파이프라인을 진행할 수 있다.

```scala
// bpu/Bpu.scala
// 현재: S3는 항상 실행되고 S1과 다를 경우 override
// 개선: S1 신뢰도 높으면 S3 완료 전에 FTQ에 committed 플래그 설정
```

#### 제안: S1 Confidence Flag 기반 Early Commit

```
S1_confidence = uBTB.strongHit && uTAGE.satCtr (강한 saturation 상태)

if (S1_confidence):
  FTQ entry → early_commit = true
  S2/S3가 도착해도 override 신호 억제 (단, 오류 시 recovery 포함)
else:
  기존 방식: S3 override 허용
```

**주의사항**:
- S1 신뢰도 판단이 틀렸을 때의 복구 비용이 크므로, early_commit 임계값을 보수적으로 설정
- 현재 uBTB(128 entry, FF)의 capacity가 작아 신뢰도 높은 hit rate가 낮을 수 있음
- uBTB 크기 확장(13.1 uBTB 관련) 병행 필요

**관련 파일**: [bpu/Bpu.scala](../bpu/Bpu.scala), [bpu/fauftb/FauFTB.scala](../bpu/fauftb/FauFTB.scala)

---

## 14. 기존 BTB/PRED 미스율·오예측률 감소 방안

> 새로운 구조 추가 없이, 기존 예측기들의 파라미터·인덱싱·학습 로직을 개선하는 방안.

---

### 14.1 mBTB: Replacement Policy 개선

#### 문제

현재 mBTB의 교체 정책은 LRU 또는 유사 방식이나, entry가 소수의 hot branch에 의해 독점될 수 있다. 특히 사용 빈도는 낮지만 오예측 페널티가 큰 cold branch들이 evict된 후 re-fetch 시 BTB miss가 발생한다.

```scala
// mbtb/MainBtbInternalBank.scala
// 현재: singlePort SRAM, write-first 정책
// 교체 정책 코드: 명시적 LRU 비트 없음 → FIFO or pseudo-random 가능성
```

#### 방안

**A. Bimodal replacement (frequency + recency 혼합)**
- 각 BTB entry에 2-bit frequency counter 추가
- 교체 시 recency(LRU)와 frequency 모두 낮은 entry를 우선 제거
- RRIP (Re-Reference Interval Prediction)과 유사 개념 BTB에 적용

**B. Protected entries for frequently-mispredicted branches**
- 오예측 플래그(`mispredict_vec`)가 최근 높았던 PC의 BTB entry에 `pinned` 비트 설정
- `pinned` entry는 교체 대상에서 제외
- **주의**: `pinned` entry 수 제한 필요 (BTB 용량의 10% 이하 권장)

**C. BTB pre-fill on ICache miss**
- ICache miss가 발생한 주소의 BTB entry를 강제로 retain (evict 금지)
- ICache miss 복귀 후 BTB miss로 이어지는 double-miss 방지

---

### 14.2 TAGE: useAltOnNa 학습 개선

#### 문제

`useAltOnNaVec`는 128 entry의 per-PC 레지스터로, provider counter가 weak일 때 alt를 사용할지 여부를 학습한다. 현재는 `cfiPC[7:1]`로만 인덱싱되므로, 같은 7-bit 구간에 있는 다른 PC들이 동일 entry를 공유한다.

```scala
// bpu/tage/Helpers.scala:41-44
def getUseAltOnNaIdx(pc: PrunedAddr): UInt =
  pc(log2Ceil(NumUseAltOnNa) - 1 + instOffsetBits, instOffsetBits)
  // = cfiPC[7:1], 7 bits, NumUseAltOnNa=128
```

#### 방안

**A. NumUseAltOnNa 증가 (128 → 512)**
```scala
// bpu/tage/Parameters.scala
NumUseAltOnNa: Int = 128  →  512
```
- 인덱스 비트: `cfiPC[9:1]` (9 bits)
- 서로 다른 PC 간 공유 감소 → per-PC 학습 품질 향상
- **비용**: 레지스터 4배 증가 (FF 기반, SRAM 아님)

**B. useAltOnNa 인덱스에 PHR 추가**
```scala
// 현재: cfiPC[7:1]
// 개선: cfiPC[6:1] XOR foldedPHR[7:2]  (총 6+6 XOR → 6 bit 인덱스)
```
- call-path별 useAltOnNa 학습
- 같은 PC라도 다른 실행 경로에서 다르게 학습 가능

---

### 14.3 RAS: Commit Stack 크기 및 speculative 복구 개선

#### 문제

현재 RAS는 speculative stack(32 entry) + commit stack(16 entry) 구조다. 깊은 콜 체인(32 단계 이상)이 있거나, speculative 복구 시 commit stack이 너무 작아 정확한 return address를 잃을 수 있다.

```scala
// bpu/ras/Parameters.scala (또는 full_ras_analysis.md 참조)
// specQueue: 32 entries
// commitStack: 16 entries
```

#### 방안

**A. commitStack 크기 증가 (16 → 32)**
- 깊은 재귀 함수 또는 깊은 콜 체인에서 RAS miss 감소
- **비용**: 레지스터 기반, 면적 선형 증가

**B. speculative push/pop 정확도 향상**
- 현재 predecoder(F2)에서 call/return 검출 후 RAS 업데이트
- 검출 오류 시 잘못된 address push → 이후 return 오예측 연쇄 발생
- 개선: **F3에서 F2 predecode 결과 재검증 후 RAS commit** (F3Predecoder 결과 활용)
- 현재 F3Predecoder는 brType/isCall/isRet를 최종 결정하지만 이 결과가 speculative RAS에 즉시 반영되는지 확인 필요

**C. Return address 압축 저장**
- 동일 함수 내 연속 call의 return address들은 base + offset으로 압축 가능
- 현재 전체 VAddrBits 저장 → high bits는 대부분 동일 → 압축으로 entry 효율화

---

### 14.4 TAGE: Skewed Associativity (해시 차별화)

#### 문제

TAGE의 각 bank에서 모든 branch가 `PC ^ foldedPHR` 동일 방식으로 인덱싱된다. 서로 다른 PC가 충돌하면 aliasing이 발생하고, 한 entry가 두 branch의 훈련에 의해 오염된다. 현재는 4 bank × 2 way 구조이나, 같은 bank 내 2 way가 모두 같은 setIdx로 접근하므로 **set 내 aliasing은 피할 수 없다**.

#### 방안: Way별 독립 해시 함수 (Skewed Associativity)

```
현재:
  bankIdx = PC[2:1]
  setIdx[way0] = PC[11:3] XOR fold(PHR, histLen)
  setIdx[way1] = PC[11:3] XOR fold(PHR, histLen)  ← way0와 동일

개선:
  setIdx[way0] = PC[11:3] XOR fold(PHR, histLen)
  setIdx[way1] = PC[11:3] XOR fold(PHR, histLen) XOR PC_scramble
                 (PC_scramble = PC[11:3] rotated by 3 bits 등)
```

- way0와 way1이 서로 다른 set에서 entry를 찾음
- 두 branch가 way0에서는 충돌하더라도 way1에서는 다른 set에 매핑될 확률이 높음
- **비용**: setIdx 계산 로직 변경, way1 SRAM 주소 경로 추가 MUX

**관련 파일**: [bpu/tage/TageTable.scala](../bpu/tage/TageTable.scala) (setIdx 계산)

---

### 14.5 mBTB + TAGE 훈련 지연 감소 (Fast-train 활성화)

#### 문제

현재 TAGE는 **commit 기반 훈련(t0_fire = FTQ resolve)**만 지원한다. mBTB 역시 speculative update가 없다. 오예측 후 올바른 branch 정보가 다시 나타나기까지 수십 cycle의 훈련 공백이 발생하며, 반복적으로 같은 branch에서 오예측이 연속으로 나는 경우 훈련이 항상 뒤처진다.

```scala
// bpu/tage/Tage.scala:189
private val t0_fire = io.stageCtrl.t0_fire && t0_hasCond && io.enable
// No fast-train path
```

#### 방안: Speculative TAGE Update (fast-train)

```
오예측이 확인된 직후 (backend redirect 발생 시점)에,
틀린 entry의 takenCtr을 즉시 반대 방향으로 1 감소:

1. redirect.valid && redirect.mispred → 해당 FTQ entry의 TAGE meta에서 providerTableIdx, wayIdx 읽기
2. takenCtr을 speculative하게 감소 (반대 방향 강화)
3. commit 시점에 정식 업데이트로 덮어씀

주의: 이중 업데이트 방지 로직 필요 (speculative update 후 commit update 시 중복 적용 금지)
```

**현재 `t0_useMeta` 최적화와의 관계**: fast-train 발동 시 meta reuse 조건 재검토 필요

**관련 파일**: [bpu/tage/Tage.scala](../bpu/tage/Tage.scala), [bpu/Bpu.scala](../bpu/Bpu.scala) (redirect 처리)

---

## 15. ICache: BE pressure 연동 동적 프리페치

> 현재 ICache 프리페치는 FTQ가 enqueue하는 즉시 prefetch 요청을 발행한다.
> Backend(Decode/Execute/Commit)의 압박 상태와 무관하게 동작하므로, BE 병목 시 ICache 프리페치 자원이 낭비된다.
> 이 섹션은 BE 상태를 ICache 프리페치 정책에 연동하는 방안을 다룬다.

---

### 15.1 IBuffer 점유율 기반 프리페치 aggressiveness 조절

#### 현재 동작

프리페치는 FTQ의 `pfPtr`가 IFU `ifuPtr`보다 앞서가면서 항상 최대 aggressiveness로 동작한다. IBuffer full 상태에서도 프리페치 파이프는 계속 WayLookup 큐를 채운다.

```scala
// ftq/NewFtq.scala
// pfPtr: toPrefetch.req.fire → pfPtr += 1
// 프리페치 제어: csr.pfEnable만 있으며, IBuffer 상태와 무관
```

#### 방안: IBuffer 점유율 → 프리페치 깊이 연동

```
IBuffer 점유율 = numValid / IBufSize (48)

구간별 프리페치 거리 (pfPtr - ifuPtr 최대값):
  0~25%:  최대 깊이 유지 (현재 동작, WayLookupSize=32 한계까지)
  25~50%: 깊이 16으로 제한 (절반)
  50~75%: 깊이 8로 제한
  75~100%: 프리페치 일시 중단 (pfPtr ≤ ifuPtr + 4)

구현:
  - IBuffer → FTQ 방향 신호: io.ibufferStatus.occupancy (3-level: low/mid/high/full)
  - FTQ 내 pfPtr 증가 조건에 occupancy 레벨 기반 gate 추가
```

**필요한 신호 추가**:
```scala
// ibuffer/IBuffer.scala → FTQ 방향 신호 추가
io.ibufferStatus.low  := numValid < (IBufSize / 4).U      // < 12
io.ibufferStatus.mid  := numValid < (IBufSize / 2).U      // < 24
io.ibufferStatus.high := numValid < (IBufSize * 3 / 4).U  // < 36
```

**기대 효과**:
- BE 병목 시 ICache MSHR 자원을 낭비하지 않음 (fetch-critical 요청에 집중)
- L2 bandwidth 절약
- 에너지 효율 향상 (불필요한 SRAM read 감소)

---

### 15.2 Backend Redirect Rate 기반 프리페치 억제

#### 문제

오예측이 빈번할 때 프리페치한 내용이 대부분 버려진다. backend redirect 이후 `icacheFlush = true`로 ICache prefetch 파이프도 flush되지만, redirect 이전에 발행된 prefetch MSHR들은 계속 L2에서 데이터를 가져온다.

```scala
// ftq/NewFtq.scala
// backend redirect → icacheFlush (ICacheMainPipe 플러시)
// ICachePrefetchPipe도 flush되지만 MSHR은 완료까지 동작 지속
```

#### 방안: Redirect 직후 Prefetch Throttle

```
redirect_rate_counter: 최근 N cycle 내 redirect 횟수를 카운트 (예: N=64)

redirect_rate_high = redirect_rate_counter > threshold  (예: 8회/64cycle)

when (redirect_rate_high):
  NumPrefetchMshr 중 절반만 활성화
  → 프리페치 MSHR 4개만 사용 (나머지 6개는 idle)
  → redirect 직후 prefetch 발행 금지 (2~4 cycle cooling period)
```

**구현 위치**: [icache/ICacheMissUnit.scala](../icache/ICacheMissUnit.scala) (MSHR 할당 로직)
**신호 연결**: FTQ의 `io.bpuInfo.mispredCnt` → ICache pfEnable-like 제어

---

### 15.3 Decode Hint 기반 타겟 주소 프리페치

#### 동기

현재 ICache 프리페치는 BPU가 예측한 주소를 FTQ를 통해 받는다. 그러나 **간접 분기 (ITTAGE miss 포함)** 의 경우, 실제 target이 decode 시점 이전까지 알 수 없다. Decode에서 레지스터 값이 결정되는 시점(execute 직전)에 target 주소를 ICache에 hint할 수 있다면, execute 완료 후 redirect 없이 이미 캐시가 warm-up된 상태를 기대할 수 있다.

#### 방안: Execute-stage Target Hint to ICache Prefetch

```
흐름:
  Execute stage: jalr의 실제 target 계산 완료
  → target이 ICache miss 예상 (iTLB + ICache 병렬 조회)
  → 프리페치 MSHR에 hint 발행
  → 실제 redirect 도착 시 ICache는 이미 해당 line을 보유
```

**현재 구조에서의 연결점**:
- `io.fromBackend.redirect.bits.cfiUpdate.target` (FTQ에 이미 있음)
- redirect 신호 발생 **1 cycle 전** `FtqRedirectAheadNum`의 ahead 신호 존재

```scala
// ftq/NewFtq.scala
// ftqIdxAhead: redirect 1-cycle 조기 신호 (backend에서)
// → 이 신호에서 redirect target을 ICache prefetch로 연결
io.toPrefetch.req.bits.startAddr := redirectAheadTarget  // 추가 경로
```

**기대 효과**: 오예측 복구 후 ICache re-fetch latency 감소

---

### 15.4 Commit Rate Adaptive Prefetch Depth

#### 동기

Backend의 ROB commit rate (cycle당 commit 수)는 IFU가 얼마나 빨리 instruction을 공급해야 하는지의 직접적 지표다. Commit이 느릴 때 ICache 프리페치를 낮추고, commit이 빠를 때 프리페치를 더 공격적으로 가져가면 자원 효율이 높아진다.

#### 방안: Commit Rate 기반 WayLookup 깊이 동적 조정

```
commitRate = PopCount(rob_commits_valid) / 1 cycle  // 0~6

평균 commitRate (8-cycle rolling window):
  high (≥ 4/cycle): WayLookup 최대 깊이 유지, NumPrefetchMshr 모두 활성
  mid (2~3/cycle):  WayLookup 깊이 16, NumPrefetchMshr 7 활성
  low (< 2/cycle):  WayLookup 깊이 8,  NumPrefetchMshr 4 활성
```

**FTQ → ICache 연결**:
```scala
// ftq/NewFtq.scala: rob_commits는 이미 수신 중
val commitRateSmooth = RegEnable(
  (commitRateSmooth * 7.U + thisCommit) >> 3.U, true.B  // IIR 필터
)
io.toPrefetch.req.bits.depth := Mux(commitRateSmooth >= 4.U, ..., ...)
```

---

## 16. Predecoder: Instruction Fusion 및 신규 Functional Unit 제안

> 현재 PreDecode.scala / F3PreDecode.scala는 instruction boundary 탐지, RVC 여부, branch type, jump offset 계산만 수행한다.
> 이 섹션은 predecoder 단에서 instruction fusion을 감지하고, 이를 활용하는 신규 functional unit 아이디어를 다룬다.

---

### 16.1 현재 Predecoder 구조 요약

```
F2 (PreDecode.scala):
  입력: 16개 half-word 스트림
  InstrBoundary: maybeRvc → instruction 경계 탐지
  PreDecode: isRvc, branchType(None/Cond/Direct/Indirect), rasAction(Push/Pop), jumpOffset
  출력: PreDecodeInfo × IBufferEnqueueWidth

F3 (F3PreDecode.scala — 현재 FIXME: maybe unused):
  입력: 32-bit expanded instructions
  출력: brAttribute (branchType, rasAction)

IBuffer에 저장되는 정보:
  cf.pd: isRVC, brAttribute
  cf.pc, cf.exceptionType, cf.trigger
```

**현재 미지원 항목**: instruction fusion, pseudo-instruction 인식, 연속 instruction 간 의존성 분석

---

### 16.2 Macro-op Fusion (가장 임팩트가 큰 방안)

#### 원리

RISC-V에서는 간단한 작업을 여러 instruction의 조합으로 표현한다. Predecoder에서 이러한 패턴을 인식하여 **하나의 fused micro-op**으로 IBuffer에 넣으면 decode/issue/execute 대역폭을 절약할 수 있다.

#### 제안 패턴

**패턴 A: LI (Load Immediate) = LUI + ADDI**
```
lui  rd, imm20         # rd = imm20 << 12
addi rd, rd, imm12     # rd = rd + imm12
→ Fused: LI rd, (imm20<<12 + imm12)
```
- 매우 흔한 패턴 (전역 변수 주소, 큰 상수)
- 두 instruction을 하나의 IBuffer entry로 병합
- 실행 시 단순 상수 로드 → ALU 1 cycle

**패턴 B: LA (Load Address) = AUIPC + ADDI**
```
auipc rd, imm20        # rd = PC + (imm20 << 12)
addi  rd, rd, imm12    # rd = rd + imm12
→ Fused: LA rd, PC_relative_addr
```
- PIC (Position-Independent Code) 에서 매우 빈번
- 두 instruction → 하나의 연산 (단순 PC+offset 계산)

**패턴 C: CALL = AUIPC + JALR**
```
auipc ra, offset_hi    # ra = PC + (offset_hi << 12)
jalr  ra, offset_lo(ra) # PC = ra + offset_lo, ra = PC+4
→ Fused: CALL target   (target = PC + offset_hi<<12 + offset_lo)
```
- 컴파일러가 생성하는 표준 함수 호출 패턴 (long call)
- Fused 후: JALR 단독과 동일 동작이지만 target이 즉시 계산됨
- **BPU에 대한 영향**: fused CALL의 target을 F2에서 미리 계산 → ITTAGE에 조기 공급 가능

**패턴 D: Zero-register NOP (x0 destination)**
```
add  x0, x1, x2  → NOP (result discarded)
addi x0, x0, 0   → canonical NOP
and  x0, ...     → NOP
→ Fused: NOP (exception check만 남김)
```
- x0 destination 명령은 side-effect(exception 제외)가 없음
- IBuffer에 넣지 않거나 빈 slot으로 처리하여 decode throughput 절약

#### 구현 위치

```
F2 (PreDecode.scala 확장):
  - consecutive(i, i+1) pair 비교: inst(i).opcode + inst(i+1).opcode
  - fusion 패턴 match → fusionValid(i) = true, fusionWith(i) = i+1
  - IBuffer enqEnable: fusionWith인 instruction은 skip

F3:
  - fused instruction의 통합 operand 계산
  - 통합된 PreDecodeInfo 생성 (brType, target 등)

IBuffer 변경:
  - fused entry 표시 비트 추가
  - Decode에 fused임을 알려 단일 uop로 처리
```

**주의사항**:
- fusion 대상 2번째 instruction(fusionWith)이 다른 branch의 target이면 fusion 불가
  (코드: `fusionValid(i) := ... && !isBranchTarget(i+1)`)
- 예외(exception) 처리: 두 instruction 각각의 exception 처리 통합 필요
- Flush/redirect 시 fused entry만 flush 됨에 주의 (중간 state 없음)

---

### 16.3 Conditional Move 패턴 인식 (Branch-to-CMOV 변환)

#### 원리

다음과 같은 if-then-else 패턴은 predecoder 수준에서 `CMOV(conditional move)` 시맨틱으로 인식 가능하다:

```
# 일반 패턴:
  beq x1, x2, taken_label  # branch
  addi x3, x4, 0            # not-taken: x3 = x4
  j    end_label
taken_label:
  addi x3, x5, 0            # taken: x3 = x5
end_label:
```

이 패턴에서 branch와 두 `addi`를 묶어서 CMOV로 처리하면:
- branch 오예측 가능성 제거
- pipeline stall 없이 연산 완료 (mux 1개)

**현실적 제약**: 이 변환은 compile-time 또는 매우 정교한 runtime 탐지가 필요하며, predecoder 단에서 실시간 탐지는 window 크기 제한으로 어려움. **더 현실적인 구현**: Decode 단에서 FTB entry + predecode 정보를 활용한 peephole 최적화.

---

### 16.4 신규 Functional Unit 제안

Fusion 탐지 결과를 실제로 처리하기 위한 backend FU 추가 방안:

#### A. Fused Load-Immediate Unit (FLIU)

```
입력: 32-bit immediate (LUI+ADDI fusion 결과)
동작: 레지스터에 즉시값 기록
latency: 0 (bypassing 가능)
적용: LI, LA fusion → 단순 register write
```

- 현재 ALU에서 처리 가능하지만, **별도 bypass path** 제공으로 critical path 단축
- LI는 dependency가 없으므로 issue 즉시 완료 가능

#### B. PC-Relative Address Unit (PRAU)

```
입력: PC + 32-bit offset (AUIPC+ADDI fusion)
동작: result = PC + sign_extend(offset)
latency: 1 cycle (adder)
적용: PIC 코드의 global variable 주소 계산
```

- 현재는 AUIPC → ALU → ADDI → ALU 2단계 필요
- Fused 처리: 단일 adder로 1 cycle 처리

#### C. 향상된 Jump Target Predictor 힌트 Port

이것은 FU 추가가 아니라 **predecoder → BPU 직접 피드백 경로** 추가:

```
F2 predecoder에서 CALL fusion 탐지 시:
  → immediate target = AUIPC + JALR immediate 합산으로 F2에서 직접 계산 가능
  → tage/ittage에 F2 시점에 target hint 전달
  → BPU가 S2 단계에서 이미 정확한 target 보유 → S3까지 기다릴 필요 없음
```

```scala
// F2에서 계산:
val callFusionTarget = f2_pc(i) + signExtend(luiImm << 12 + jalrImm)
// BPU hint 경로:
io.toFtq.pdWb.bits.fusionCallTarget := callFusionTarget
// BPU가 이 정보를 S2 prediction override에 사용
```

---

### 16.5 Zero-Bubble Branch via Pre-resolved Branches

#### 원리

Predecoder에서 branch의 조건을 **즉각 해결 가능한 경우** 감지하면, branch 예측 자체를 생략할 수 있다.

**즉각 해결 가능한 branch 패턴**:

```
# 항상 taken인 branch:
  beq  x0, x0, label     # x0==x0 항상 참 → 무조건 분기 (JAL와 동일)

# 항상 not-taken인 branch:
  bne  x0, x0, label     # x0!=x0 항상 거짓 → never taken
  blt  x0, x0, label     # 항상 not taken
```

이 패턴들은 predecoder에서 탐지하여:
1. BPU 예측 없이 F2에서 immediately resolved
2. FTQ에 `predResolved=true` 플래그 기록
3. BPU가 훈련 데이터를 낭비하지 않음

**더 일반적인 경우**: 컴파일러가 `__builtin_expect(0)`로 마킹한 경우 → 향후 Zicond 확장과 연계 가능

---

### 16.6 구현 우선순위 및 복잡도 평가

| 방안 | 기대 효과 | 구현 복잡도 | 호환성 영향 |
|---|---|---|---|
| LUI+ADDI → LI fusion | 중 (decode bandwidth 절약) | 중간 | IBuffer entry 형식 변경 필요 |
| AUIPC+ADDI → LA fusion | 중 | 중간 | 위와 동일 |
| AUIPC+JALR → CALL fusion + BPU hint | 상 (ITTAGE S2 조기 제공) | 높음 | FTQ pdWb + BPU 경로 추가 |
| x0-dest NOP 제거 | 낮-중 | 낮음 | enqEnable mask만 변경 |
| Loop Predictor (13.1) | 중-상 | 높음 | BPU S2 슬롯 추가 |
| Pre-resolved branch | 낮-중 | 낮음 | FTQ entry 플래그 추가 |
| CMOV 변환 | 상 | 매우 높음 | Decode + Execute 대규모 변경 |

**권장 순서**: x0-dest NOP 제거 → LI/LA fusion → CALL fusion+BPU hint → Loop Predictor

---

*분석 기반: ifu/PreDecode.scala, ifu/InstrBoundary.scala, ifu/F3PreDecode.scala, ifu/PredChecker.scala, bpu/Bundles.scala (BranchAttribute), ftq/Ftq.scala, ibuffer/IBuffer.scala + 전체 doc/*.md*
