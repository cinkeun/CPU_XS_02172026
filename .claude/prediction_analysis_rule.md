# prediction_analysis_rule.md

> 최우선 원칙: 모든 분석은 반드시 **code-based only**로 수행한다.  
> `web-search` 결과나 사전 지식(기억 기반 정보)에 의존한 추론/서술은 금지한다.

> 목적: prediction unit(TAGE, bimodal, perceptron 등)을 동일한 포맷으로 분석해, 구조/메모리/BTB pairing/latency/training을 일관되게 문서화한다.

---

## 0) 기본 원칙

* 분석 근거는 Scala/Chisel 코드의 선언/연결/업데이트 로직만 사용한다.
* prediction unit별로 아래 `1.1 ~ 1.8` 순서를 반드시 지킨다.
* 표, pseudocode, 코드 인용(snippet) 3가지를 반드시 포함한다.
* 수치(depth/width/tables/banks/ports/latency/throughput)는 코드에서 직접 추론 가능한 값만 기재한다.

---

## 1) Prediction Unit 분석 템플릿 (필수 순서)

### 1.1 prediction unit 종류 및 역할

아래를 반드시 기재:

* prediction unit 종류: `TAGE`, `bimodal`, `perceptron` 등
* entry 당 control flow instruction 처리 단위 수
* 예측 거리:
  * 현재 input block의 "다음 block" 예측인지
  * "더 먼 block"(lookahead) 예측인지

권장 표:

| Unit | Type | CFI per Entry | Predict Distance | 설명 |
| --- | --- | --- | --- | --- |

---

### 1.2 prediction unit memory spec

아래를 반드시 기재:

* memory depth
* memory width (bit)
* number of tables
* number of banks
* number of write ports
* number of read ports

권장 표:

| Memory/Table | Depth | Width(bit) | #Tables | Banks | Read Ports | Write Ports |
| --- | --- | --- | --- | --- | --- | --- |

---

### 1.3 prediction unit memory entry 설명 (코드 포함 필수)

아래를 반드시 기재:

* entry 정의 코드 snippet (`class/Bundle/Struct`)
* 각 field의 name, width, description

필수 포맷:

```scala
// Source: <path>:<line>
// prediction unit entry definition snippet
...
```

권장 표:

| Field Name | Width(bit) | Description |
| --- | --- | --- |

---

### 1.4 pair BTB unit 설명

prediction unit와 짝으로 동작하는 BTB를 기재:

* pairing BTB 종류 (예: uBTB, aBTB, mBTB 등)
* pairing 목적 (direction-only / direction+position / target 보정 등)
* 결합 포인트(stage, signal)

권장 표:

| Prediction Unit | Paired BTB | Pairing Purpose | 결합 Stage/Signal |
| --- | --- | --- | --- |

---

### 1.5 다음 예측 pseudocode (paired BTB 포함)

prediction unit 단위로 pseudocode를 반드시 작성:

* 입력: start PC, history/meta, paired BTB output
* 처리: hit/miss, provider/alt 선택, BTB position/target 결합
* 출력: taken/not-taken, position, target(또는 target 선택 신호), meta

필수 포맷:

```text
onPredict(input, pairedBTB):
  ...
```

---

### 1.6 input-to-output latency 및 throughput

아래를 반드시 수치로 기재:

* input to output cycle latency
* prediction unit throughput (predictions/cycle)

권장 표:

| Unit | Input Stage | Output Stage | Latency(cycle) | Throughput(pred/cycle) |
| --- | --- | --- | --- | --- |

---

### 1.7 pipeline stage 위치 (입력/출력 타이밍)

아래를 반드시 기재:

* input이 전체 stage 중 어디(s0/s1/s2...)에서 들어오는지
* output이 어느 cycle/stage에서 유효해지는지
* stage 경계 레지스터(`RegNext`, `RegEnable`, `Queue`) 근거

권장 표:

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
| --- | --- | --- | --- |

---

### 1.8 Training 방법

아래를 반드시 기재:

* training trigger 시점(예: resolve, mispredict, commit, fast-train)
* FTQ가 보관해야 하는 정보 목록
* FTQ 내 어느 memory/queue/meta 영역에 저장되는지
* meta info field를 코드 snippet으로 제시
* write 시 port congestion 처리 방식(arbiter, queueing, stall, multi-cycle writeback 등)

필수 포맷:

```scala
// Source: <path>:<line>
// training meta fields snippet
...
```

권장 표:

| Trigger | Required FTQ Info | FTQ Storage | Write Port/Conflict Handling |
| --- | --- | --- | --- |

---

## 2) 품질 체크리스트

* [ ] 1.1~1.8 순서 준수
* [ ] memory depth/width/tables/banks/read/write ports 명시
* [ ] memory entry 코드 snippet 포함
* [ ] field별 width/description 표 완성
* [ ] pair BTB 결합 규칙 명시
* [ ] paired BTB 포함 pseudocode 작성
* [ ] latency/throughput 수치화
* [ ] stage 입력/출력 타이밍 명시
* [ ] training trigger/FTQ 저장/meta fields/port conflict 처리 명시

---

### END
