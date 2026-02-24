# prediction_analysis_rule.md

> 최우선 원칙: 모든 분석은 반드시 **code-based only**로 수행한다.  
> `web-search` 결과나 사전 지식(기억 기반 정보)에 의존한 추론/서술은 금지한다.

> 목적: BTB 기반 예측기(Region/Block 포함)를 동일한 포맷으로 분석해, 구조/메모리/짝 예측기/latency/override-redirection 동작을 일관되게 문서화한다.

---

## 0) 기본 원칙

* 분석 근거는 Scala/Chisel 코드의 선언/연결/갱신 로직만 사용한다.
* BTB별로 아래 `1.1 ~ 1.10` 순서를 반드시 지킨다.
* 표, pseudocode, 코드 인용(원문 snippet) 3가지를 반드시 포함한다.
* 수치(depth/width/ports/latency/throughput)는 코드에서 직접 추론 가능한 값만 기재한다.

---

## 1) BTB 분석 템플릿 (필수 순서)

### 1.1 BTB 종류 및 역할

아래를 반드시 기재:

* BTB 종류: `Region` 또는 `Block`
* BTB block width
* 예측 거리:
  * 현재 input block의 "다음 block" 예측인지
  * "더 먼 block"(lookahead) 예측인지

권장 표:

| BTB | Type(Region/Block) | Block Width | Predict Distance | 설명 |
| --- | --- | --- | --- | --- |

---

### 1.2 BTB memory spec

아래를 반드시 기재:

* memory depth
* memory width (bit)
* number of banks
* number of write ports
* number of read ports

권장 표:

| Memory | Depth | Width(bit) | Banks | Read Ports | Write Ports |
| --- | --- | --- | --- | --- | --- |

---

### 1.3 BTB memory entry 설명 (코드 포함 필수)

아래를 반드시 기재:

* entry 정의 코드 snippet (`class/Bundle/Struct`)
* 각 field의 name, width, description

필수 포맷:

```scala
// Source: <path>:<line>
// BTB entry definition snippet
...
```

권장 표:

| Field Name | Width(bit) | Description |
| --- | --- | --- |

---

### 1.4 Pair prediction unit

BTB와 짝으로 동작하는 예측 유닛을 기재:

* 예: `bimodal`, `tage`, `sc`, `perceptron` 등
* 각 유닛이 개입하는 결정 포인트(방향/타겟/confidence/flip 등)

권장 표:

| BTB | Paired Unit | 역할 | 결합 방식 |
| --- | --- | --- | --- |

---

### 1.5 다음 예측 pseudocode

BTB 단위로 pseudocode를 반드시 작성:

* 입력: start PC, history/meta, redirect/override 신호
* 처리: hit/miss, taken/not-taken, pair unit 결합
* 출력: next block/target, valid, metadata

필수 포맷:

```text
onPredict(input):
  ...
```

---

### 1.6 Input-to-output latency 및 throughput

아래를 반드시 수치로 기재:

* input to output cycle latency
* BTB throughput (predictions/cycle)

권장 표:

| BTB | Input Stage | Output Stage | Latency(cycle) | Throughput(pred/cycle) |
| --- | --- | --- | --- | --- |

---

### 1.7 Pipeline stage 위치 (입력/출력 타이밍)

아래를 반드시 기재:

* input이 전체 stage 중 어디(s0/s1/s2...)에서 들어오는지
* output이 어느 cycle/stage에서 유효해지는지
* stage 경계 레지스터(`RegNext`, `RegEnable`, `Queue`) 근거

권장 표:

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
| --- | --- | --- | --- |

---

### 1.8 BTB memory indexing hashing 방법

아래를 반드시 기재:

* lookup index 생성식 (setIdx/bankIdx/way 선택 기준)
* history 사용 여부:
  * 사용 시: 어떤 history(예: GHR/PHR/path/folded history)를 쓰는지
  * 미사용 시: `history 없음`을 명시
* PC/history의 bit position:
  * PC에서 어떤 bit range를 쓰는지 (예: `pc[15:8]`)
  * history에서 어떤 bit 또는 folded 값을 쓰는지
* hash 결합 방식:
  * XOR/concat/fold/rotate 등 실제 연산식을 코드 근거로 제시
* predict path와 train/update path에서 index/hash가 다르면 둘 다 분리 기술

필수 포맷:

```scala
// Source: <path>:<line>
// index/hash generation snippet
...
```

권장 표:

| Path(Predict/Train) | Index/Hash Name | Formula | PC Bits | History Bits | Note |
| --- | --- | --- | --- | --- | --- |

---

### 1.9 Training 방법

아래를 반드시 기재:

* training trigger 시점(예: resolve, mispredict, commit, fast-train)
* FTQ가 가지고 있어야 할 정보 목록
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

### 1.10 Override 및 redirection

아래를 반드시 기재:

* 다른 BTB/다른 predictor와 충돌 시 override 우선순위
* entry 불일치/더 강한 예측 도착 시 override 조건
* override 발생 후 redirection 경로와 타이밍

필수 포함 항목:

* 우선순위 규칙 (예: `redirect > s3_override > s1_prediction`)
* flush/replay와의 상호작용
* 최종 nextPC 선택 로직의 코드 근거

권장 표:

| Condition | Winner | Redirect Target | Side Effect(Flush/Replay) |
| --- | --- | --- | --- |

---

## 2) 품질 체크리스트

* [ ] 1.1~1.10 순서 준수
* [ ] memory depth/width/banks/read/write ports 명시
* [ ] memory entry 코드 snippet 포함
* [ ] field별 width/description 표 완성
* [ ] pair predictor 결합 규칙 명시
* [ ] pseudocode 포함
* [ ] latency/throughput 수치화
* [ ] stage 입력/출력 타이밍 명시
* [ ] indexing/hash 식 + PC/history bit position 명시
* [ ] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
* [ ] override/redirection 우선순위 및 근거 명시

---

### END
