# RAS_analysis_rule.md

> 최우선 원칙: 모든 분석은 반드시 **code-based only**로 수행한다.  
> `web-search` 결과나 사전 지식(기억 기반 정보)에 의존한 추론/서술은 금지한다.

> 목적: Open-source CPU Scala/Chisel 코드에서 **RAS(Return Address Stack)** 구현을, 코드 위치를 모르는 상태에서도 일관된 포맷으로 분석/리뷰할 수 있게 한다.

---

## 0) 기본 원칙

* 분석 근거는 Scala/Chisel 코드의 선언/연결/업데이트/복구 로직만 사용한다.
* 문서에는 표, pseudocode, 코드 snippet을 반드시 포함한다.
* 수치(depth/width/tables/banks/ports/latency/throughput)는 코드에서 직접 확인 가능한 값만 기재한다.
* RAS와 ITTAGE/BTB가 함께 타겟 후보를 낼 경우, 최종 선택 우선순위를 반드시 명문화한다.

---

## 1) 분석 산출물 규격 (결과 리포트 템플릿)

### 1.1 요약

아래 항목을 반드시 기재:

* 코어/레포/커밋
* RAS depth
* ITTAGE 존재 여부
* 업데이트 방식 (Speculative / Commit-only / Hybrid)
* 복구 방식 (Checkpoint / History buffer / Full reset / 기타)
* RAS의 연속적인 prediction 실패에 따른 복구방법이 있는지 분석
* Ret 타겟 우선순위 정책 (RAS 우선 / ITTAGE 우선 / 조건부)
* Multi-fetch 동시 call/ret 처리 방식 (Single-event / Multi-event / Partial)
* 핵심 리스크 Top 3

### 1.2 관찰 기반 인터페이스 기록

실제로 확인한 신호명을 기록:

* RAS 관련 예: `ras_target`, `ras_valid`, `ras_push`, `ras_pop`, `ras_sp`, `ras_top`
* ITTAGE 관련 예: `ittage_target`, `ittage_hit`, `indirect_target`

### 1.3 동작 규칙 명문화

아래 규칙을 한글 문장으로 명확히 기술:

* Call 판별 규칙
* Return 판별 규칙
* Push 규칙
* Pop 규칙
* Flush/Redirect 시 복구 규칙
* ITTAGE/BTB와 충돌 시 선택 규칙
* 동일 fetch block 내 다중 call/ret 동시 발생 규칙

### 1.4 이슈 목록 포맷

각 항목을 아래 형식으로 기록:

* ID: `RAS-XXX`
* 심각도: `Blocker` / `High` / `Medium` / `Low` / `Info`
* 증상
* 근거(신호/조건/코드 snippet)
* 개선 제안

### 1.5 다음 예측 pseudocode (paired BTB 포함)

prediction unit 단위로 pseudocode를 반드시 작성:

* 입력: `startPC`, `history/meta`, `paired BTB output`
* 처리: hit/miss, provider/alt 선택(해당 시), BTB position/target 결합
* 처리: RAS/ITTAGE 동시 hit 시 우선순위 반영
* 출력: taken/not-taken, position, target(또는 선택 신호), meta

필수 포맷:

```text
onPredict(input, pairedBTB):
  ...
```

### 1.6 Input-to-output latency 및 throughput

아래를 반드시 수치로 기재:

* input-to-output cycle latency
* throughput (predictions/cycle)

권장 표:

| Unit | Input Stage | Output Stage | Latency(cycle) | Throughput(pred/cycle) |
| --- | --- | --- | --- | --- |
| RAS |  |  |  |  |
| ITTAGE (if any) |  |  |  |  |
| Paired BTB |  |  |  |  |
| Final select / MUX |  |  |  |  |

기재 규칙:

* latency: 입력 유효 시점부터 최종 taken/target 유효 시점까지 cycle 수
* throughput: steady-state 기준 예측 처리율 (예: `1 pred/cycle`, `0.5 pred/cycle`)

### 1.7 Pipeline stage 위치 (입력/출력 타이밍)

아래를 반드시 기재:

* input이 들어오는 stage (`s0/s1/s2...`)
* output이 유효해지는 stage/cycle
* stage 경계 레지스터 근거 (`RegNext`, `RegEnable`, `Queue`)

권장 표:

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
| --- | --- | --- | --- |
| startPC |  |  |  |
| pairedBTB.hit/pos/target |  |  |  |
| ras_target/valid |  |  |  |
| ittage_target/hit |  |  |  |
| final_taken/pos/target |  |  |  |
| redirect/flush |  |  |  |

### 1.8 Training 방법

아래를 반드시 기재:

* training trigger 시점 (resolve / mispredict / commit / fast-train)
* FTQ가 보관해야 하는 정보
* FTQ 내 저장 위치 (entry field / meta RAM / queue)
* meta field 코드 snippet
* write port congestion 처리 방식 (arbiter/queue/stall/multi-cycle)

필수 코드 포맷:

```scala
// Source: <path>:<line>
// training meta fields snippet
...
```

권장 표:

| Trigger | Required FTQ Info | FTQ Storage | Write Port/Conflict Handling |
| --- | --- | --- | --- |

#### 1.8.1 training 트리거 (필수)

* resolve 기반
* mispredict 기반
* commit 기반
* fast-train 기반

각 트리거별로 아래를 기록:

* 어떤 predictor가 학습하는지 (RAS/ITTAGE/BTB)
* 학습 입력 (`pc`, `target`, `taken`, `provider`, `history`, `meta`)
* speculative/non-speculative 여부

#### 1.8.2 FTQ 저장 정보 (필수)

최소 체크 항목:

* `startPC` / fetch packet PC
* selected provider (`RAS`/`ITTAGE`/`BTB`/`NONE`)
* predicted taken/target/position
* predictor별 meta (`ittage meta`, `btb meta`, etc.)
* RAS meta (`topIdx`, `push/pop event`, `checkpoint id`, etc.)
* history snapshot (존재 시)

#### 1.8.3 FTQ 내 저장 위치 (필수)

아래 중 어떤 구조인지 명시:

* FTQ entry field 직접 저장
* 별도 meta RAM/queue 저장
* FTQ index 기반 병렬 메모리 참조

#### 1.8.4 write port congestion 처리 (필수)

아래 중 실제 구현 방식을 코드 근거와 함께 기록:

* arbiter multiplexing
* queue buffering
* stall/backpressure
* multi-cycle writeback
* multi-write-port

---

## 2) 코드 위치를 몰라도 시작하는 탐색 규칙

### 2.1 1차 탐색 키워드

* RAS: `RAS`, `ReturnAddr`, `ReturnAddress`, `retStack`, `rstack`
* call/ret: `ret`, `call`, `jal`, `jalr`, `link`, `ra`, `x1`
* predictor: `predict`, `bp`, `bpu`, `btb`, `tage`, `ittage`, `indirect`
* recovery: `redirect`, `flush`, `rollback`, `kill`, `restore`, `checkpoint`
* multi-fetch: `fetchWidth`, `fetchBytes`, `bank`, `slot`, `lane`, `bundle`, `block`

### 2.2 2차 탐색 패턴

* stack 구조: `Vec(depth, ...)`, `Reg(Vec(...))`, `Mem(depth, ...)` + `sp/top/idx`
* push/pop 패턴: `when(push)`, `when(pop)`, `idx := idx + 1`, `idx := idx - 1`
* 타겟 선택 MUX: `Mux(isRet, rasTarget, ...)`
* slot별 decode: `isCall(i)`, `isRet(i)`, `slot(i)`

---

## 3) 기능 체크리스트

* Call/Ret 판별 정확성
* Push 주소 계산 정확성 (`PC+4`, compressed ISA 보정 등)
* speculative update와 rollback 일관성
* redirect/flush 우선순위 일관성
* underflow/overflow 처리
* RAS의 연속적인 prediction 실패에 따른 복구방법이 있는지 분석
* ITTAGE 동시 hit 시 우선순위 일관성
* 동일 fetch block 내 다중 call/ret 동시 처리 (순서/제한/selectedIdx)

---

## 4) 동일 fetch block 내 다중 call/ret 동시 처리 (핵심)

아래를 반드시 명시:

* 한 cycle에 처리 가능한 최대 이벤트 수 (call/ret 각각)
* 다중 이벤트 발생 시 선택 규칙 (예: first/last/highest-priority slot)
* 같은 cycle에 push와 pop이 모두 있을 때의 순서 (push-first / pop-first / cancel-out)
* block 내 slot 순서 기반 누적 업데이트 가능 여부

최소 검증 케이스:

* `call_call`
* `call_ret`
* `ret_call`
* `ret_ret`
* 동일 slot에서 call/ret 동시 true (디코드/우선순위 충돌)

---

## 5) RAS memory 구조 분석

### 5.1 memory spec (필수)

아래를 반드시 기재:

* memory depth
* memory width (bit)
* number of tables
* number of banks
* number of read ports
* number of write ports

권장 표:

| Memory/Table | Depth | Width(bit) | #Tables | Banks | Read Ports | Write Ports |
| --- | --- | --- | --- | --- | --- | --- |
| RAS |  |  |  |  |  |  |

### 5.2 memory update/recovery 경로

아래를 코드 근거로 정리:

* predict 시 speculative push/pop 경로
* flush/redirect 시 pointer/data 복구 경로
* commit 시 확정 경로(있다면)

---

## 6) RAS memory entry 설명 (코드 포함 필수)

### 6.1 entry 정의 코드 snippet

필수 포맷:

```scala
// Source: <path>:<line>
// RAS entry definition snippet
...
```

### 6.2 entry field 설명

권장 표:

| Field Name | Width(bit) | Description |
| --- | --- | --- |

---

## 7) 검증(테스트) 기준

* 기본 call/ret + nested call/ret
* overflow/underflow
* redirect 인접 cycle의 ret 처리
* 동일 fetch block 다중 이벤트 (`call_call`, `call_ret`, `ret_call`, `ret_ret`)
* 동일 slot call+ret 동시 true 케이스
* ITTAGE/RAS 동시 hit + target 불일치 케이스
* RAS의 연속적인 prediction 실패에 따른 복구방법이 있는지 분석 (예: flush 후 checkpoint 복원, fallback predictor 전환, RAS reset/re-init 조건)
* training write burst 시 port congestion 케이스(가능 시)

---

## 8) 심각도 분류 룰

* `Blocker`: 잘못된 복구로 fetch가 오주소로 지속 진행
* `High`: ret 반복 mispredict 및 RAS 오염 지속
* `Medium`: 정책 불명확/다중 이벤트 누락으로 정확도 저하
* `Low`: 문서/가독성/테스트 부족
* `Info`: 계측(counter) 추가, 리팩토링 제안

---

## 9) 품질 체크리스트

* [ ] code-based only 준수
* [ ] 1.1~1.8 산출물 완성
* [ ] pseudocode + 표 + 코드 snippet 포함
* [ ] RAS/ITTAGE/BTB 최종 타겟 선택 규칙 명시
* [ ] latency/throughput 수치화
* [ ] stage 타이밍 근거(`RegNext`/`RegEnable`/`Queue`) 제시
* [ ] training trigger/FTQ/meta/write-congestion 설명
* [ ] 다중 call/ret 동시 처리 규칙 및 테스트 포함

---

### END
