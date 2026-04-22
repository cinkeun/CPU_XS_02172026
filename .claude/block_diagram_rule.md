# block_diagram_rule.md

> 최우선 원칙: 모든 다이어그램은 반드시 **code-based only**로 작성한다.  
> `web-search` 결과나 기억 기반 설명으로 블록을 추가/삭제/해석하는 것은 금지한다.

> 목적: RTL 또는 RTL에 준하는 구조적 코드(Verilog, SystemVerilog, VHDL, Chisel/Scala 등)를 읽고, 상위 블록의 **구조**, **주요 하위 블록**, **블록 간 인터페이스**, **데이터/제어/백프레셔 방향**을 draw.io block diagram으로 일관되게 표현한다.

---

## 0) 기본 원칙

### 0.1 적용 범위

이 규칙은 특정 언어 전용이 아니다. 아래 입력을 모두 포함한다.

* Verilog
* SystemVerilog
* VHDL
* Chisel / Scala RTL
* 그 외 구조적 HW 기술 언어

문서 안의 예시는 일부 언어 문법을 사용할 수 있지만, **최종 규칙 해석은 언어 중립적**이어야 한다.

### 0.2 다이어그램의 역할

이 규칙의 산출물은 flowchart가 아니라 **structural block diagram**이다.  
즉 "시간 순서"보다 아래를 우선 표현해야 한다.

* 상위 블록 안에 어떤 하위 블록이 있는가
* 블록 사이에 어떤 인터페이스가 오가는가
* 데이터는 어느 방향으로 흐르는가
* ready/valid, flush, stall 같은 제어는 어디로 되돌아가는가

파이프라인 타이밍이 핵심이면 sequence diagram이나 별도 분석 문서에서 다룬다.  
block diagram에는 **구조적으로 중요한 sub-block과 interface**를 넣는다.

### 0.3 code-based only 근거

다이어그램에 들어가는 모든 블록/화살표는 아래 코드 단서 중 하나 이상으로 근거를 가져야 한다.

#### 블록 근거

언어와 무관하게, 아래와 같은 **구조적 인스턴스화**를 블록 근거로 본다.

* submodule / component / entity / module instantiation
* generated instance
* architecturally meaningful memory declaration
* named pipeline stage block
* top-level external peer와 직접 연결되는 구조적 wrapper

언어별 예시:

* Verilog/SystemVerilog
  * `foo u_foo (...)`
  * `generate ... endgenerate`
* VHDL
  * `u_foo : entity work.foo port map (...)`
  * `component ... port map`
* Chisel/Scala
  * `Module(new Foo)`
  * `LazyModule(new Foo)`

#### 인터페이스 근거

아래와 같은 포트/인터페이스 선언 및 연결을 근거로 사용한다.

* module/entity/component port
* interface / bundle / record / struct
* ready-valid류 handshake
* req/resp pair
* bus interface
* explicit wire/signal assignment crossing block boundary

언어별 예시:

* Verilog/SystemVerilog
  * `input`, `output`, `inout`, `interface`, `modport`
  * `assign`, `always_comb`, named port connection
* VHDL
  * `in`, `out`, `inout`, `buffer`
  * `signal`, `port map`
* Chisel/Scala
  * `IO`, `Bundle`, `DecoupledIO`, `ValidIO`, `Flipped`, `<>`, `:=`

#### 파이프라인/제어 근거

아래와 같은 단서를 stage/control 표현 근거로 사용한다.

* stage naming: `s0/s1/s2`, `stage0/stage1`, `pipe0/pipe1`
* explicit register boundary
* queue / fifo / skid buffer / pipe buffer
* state machine
* `ready`, `valid`, `fire`
* `flush`, `stall`, `kill`, `redirect`, `replay`

언어별 예시:

* Verilog/SystemVerilog
  * `always_ff`, pipeline reg, `fifo`, `state_t`
* VHDL
  * clocked process, register signal, enumerated state
* Chisel/Scala
  * `RegNext`, `RegEnable`, `Queue`, `Pipe`

### 0.4 다이어그램에 넣을 것 / 생략할 것

#### 반드시 포함

* 상위 블록과 직접 연결되는 architecturally meaningful sub-block
* top-level external interface 또는 외부 peer
* data path의 핵심 하위 블록
* control/backpressure의 핵심 경로
* memory macro 또는 memory array가 구조적으로 중요한 경우
* pipeline stage가 블록 이해에 핵심인 경우

#### 원칙적으로 생략

* 단순 utility logic
  * trivial mux
  * encoder/decoder
  * one-line glue logic
  * local wire only helper
* 구조 이해에 영향이 없는 helper function/object/package
* leaf combinational expression 자체

단, 아래 조건이면 포함할 수 있다.

* arbitration이 병목이나 우선순위 정책을 결정함
* queue/buffer가 backpressure를 결정함
* error path / flush path에서 핵심 역할을 함
* memory access ordering에 실질적 영향이 있음

### 0.5 블록 이름 규칙

같은 block type이 여러 번 instantiate될 수 있으므로, 다이어그램 박스에는 **instance name + block type**를 같이 적는 것을 기본으로 한다.

권장 포맷:

```text
instanceName
BlockType
short role
```

예:

```text
mainPipe
ICacheMainPipe
serves fetch requests
```

언어 중립적으로 해석하면:

* instance name = 인스턴스 식별자
* block type = module/entity/class/component 이름

다만 top-level diagram이 너무 복잡하면 아래 규칙을 따른다.

* 동일 타입 1개만 있으면: block type 중심 표기 가능
* 동일 타입 2개 이상이면: 반드시 instance name 포함

### 0.6 외부 I/O 표기 규칙

top-level block diagram에서는 내부 sub-block만 그리지 말고, **외부와의 경계**도 반드시 표현한다.

최소 포함 대상:

* 상위 블록의 입력 인터페이스
* 상위 블록의 출력 인터페이스
* 외부 memory / bus / CSR / TLB / scheduler / frontend / backend처럼 구조상 중요한 peer

외부 peer는 보통 회색 또는 얇은 테두리 박스로 그린다.

---

## 1) Output Format

### 1.1 산출물 형식

* **File format**: draw.io (`.drawio` XML)
* **Diagram type**: block diagram
* **One page by default**
* 너무 복잡하면 page를 나누되, 다음 순서를 권장한다.
  * Page 1: top-level structure
  * Page 2: internal pipeline/memory detail

### 1.2 다이어그램 제목

다이어그램 상단 title은 분석 대상 상위 블록의 정확한 이름을 사용한다.

예:

* `ICache`
* `FTQ`
* `BPU`
* `dma_ctrl`

파일명과 title이 다를 경우, title은 **코드상 상위 블록 이름**을 우선한다.

---

## 2) Diagram Contents

### 2.1 Sub-Blocks

각 sub-block에 대해 아래를 표시한다.

* **Instance name**
* **Block type**
* **Role**: 짧은 설명 한 줄

권장 설명 길이:

* 3~8 words
* 문장보다는 명사구

예:

* `handles miss requests`
* `stores meta tags`
* `updates replacement state`

### 2.2 Interfaces Between Sub-Blocks

각 블록 경계를 넘는 인터페이스에 대해 아래를 표시한다.

* **Name**: 코드의 exact signal/port/interface/bundle name 사용
* **Direction**: 화살표 방향으로 표현
* **Type/protocol**: 필요 시 `Decoupled`, `Valid`, `AXI`, `TL`, `req/resp`, `record`, `interface` 등을 부기
* **Grouping**: 같은 interface/bundle/record이면 가능한 한 하나의 화살표로 묶기

#### 묶어야 하는 경우

* `req`, `resp`처럼 프로토콜 단위가 명확한 경우
* interface/record/bundle 전체가 함께 이동하는 경우
* 개별 field가 같은 source/target으로 함께 이동하는 경우

#### 풀어야 하는 경우

* interface field 일부만 다른 destination으로 가는 경우
* `ready/valid`처럼 제어 방향을 별도로 보여줘야 이해가 쉬운 경우
* error / flush / redirect가 data path와 다른 방향으로 흐르는 경우

### 2.3 데이터/제어/백프레셔를 구분해서 표현

화살표는 최소 아래 세 종류로 구분한다.

* **Data / request / response**: 실선
* **Control / flush / redirect / exception**: 점선
* **Backpressure / ready / stall**: 점선 또는 다른 색상

handshake 계열에서 한 개 화살표만 쓸지, 두 개를 나눌지는 아래 기준을 따른다.

#### 한 개 화살표로 충분한 경우

* request interface 전체를 한 번에 보여줄 때
* response interface 전체를 한 번에 보여줄 때
* bus channel 자체를 한 번에 보여줄 때

#### 분리해야 하는 경우

* forward data와 backward ready/stall 방향이 구조적으로 중요할 때
* backpressure가 성능 병목 설명의 핵심일 때
* control/flush path를 따로 드러내야 할 때

예:

```text
A ----req----> B
B - - - ready/stall - - -> A
```

### 2.4 Memory 블록 표현

메모리 구조가 핵심이면 memory도 sub-block으로 표현한다.

포함 권장 조건:

* banked memory
* multi-port memory
* read/write arbitration이 구조적으로 중요
* tag/data/meta array처럼 block의 본질적 구성요소

memory 박스에는 가능하면 아래를 포함한다.

* memory name
* type (`SRAM`, `RAM`, `FIFO`, `Array`, `Queue`)
* spec 요약
  * `256x66b`
  * `8 banks`
  * `single-port`

---

## 3) Derivation Rules

아래 표를 기본 매핑 규칙으로 사용한다.

| Source construct | Diagram element |
| --- | --- |
| submodule/entity/component instantiation | sub-block |
| generated instance | sub-block |
| architecturally meaningful memory declaration | memory sub-block |
| port / interface / bundle / record field | interface arrow |
| handshake / req-resp / bus declaration | protocol-labeled interface |
| queue / fifo / explicit pipeline register boundary | optional pipeline/buffer sub-block |
| signal assignment / port map / connection statement | connectivity evidence |
| nearby comment / descriptive name | role subtitle evidence |

### 3.1 연결 문법을 기계적으로 해석하지 않는다

언어마다 연결 문법이 다르므로, 특정 문법 하나를 기계적으로 방향으로 바꾸면 안 된다.

예를 들어:

* Verilog named port connection
* VHDL `port map`
* Chisel `<>`

이들은 모두 "연결"의 근거일 뿐, **최종 데이터 방향은 port/interface direction 정의**로 판단해야 한다.

즉:

* 연결 문장을 봤다고 바로 양방향 화살표로 그리면 안 된다.
* 반드시 source/sink를 추가 확인해야 한다.

### 3.2 assignment 방향 해석 규칙

assignment는 값이 어느 쪽에서 어느 쪽으로 전달되는지에 대한 근거다.  
다만 diagram에는 **block boundary를 넘는 최종 연결**만 그린다.

언어별 예시:

* Verilog/SystemVerilog
  * `assign lhs = rhs;`
  * `lhs <= rhs;`
* VHDL
  * `lhs <= rhs;`
* Chisel/Scala
  * `lhs := rhs`

공통 규칙:

* `rhs -> lhs` 흐름으로 해석
* 단, 중간 local signal은 생략 가능
* block boundary를 넘는 의미 있는 연결만 남긴다

### 3.3 instance와 block type 동시 표기 규칙

예를 들어:

* Verilog
  * `foo u_foo (...)`
* VHDL
  * `u_foo : entity work.foo port map (...)`
* Chisel
  * `val fooInst = Module(new Foo)`

이 경우 다이어그램에는 아래 둘 다 남아야 한다.

* instance: `u_foo`, `fooInst`
* type: `foo`, `Foo`

그냥 type만 적으면 동일 type 여러 개가 있을 때 다이어그램이 모호해진다.

### 3.4 stage는 아무 때나 블록으로 승격하지 않는다

아래 경우에만 stage/sub-stage를 별도 박스로 표현한다.

* 코드에 명시적 stage naming이 있음
* stage 간 register/queue 경계가 명확함
* 그 stage 구분이 block 이해에 핵심

그 외에는 block 내부의 role 설명으로 충분하다.

---

## 4) Layout Guidelines

### 4.1 기본 방향

우세한 dataflow 방향으로 정렬한다.

* request/forward path가 좌→우면 전체를 좌→우
* pipeline이 위→아래로 읽히는 구조면 위→아래

한 장 안에서 좌→우와 위→아래를 섞는 것은 최소화한다.

### 4.2 배치 우선순위

권장 배치 순서:

1. external input peers
2. ingress / request handling blocks
3. central processing / array / pipeline blocks
4. egress / response blocks
5. backward control paths

### 4.3 선 교차 최소화

다음 원칙을 지킨다.

* data path는 가장 곧고 짧게
* control/backpressure는 우회 가능
* crossing이 unavoidable이면 control arrow를 우회
* memory read/write는 가능하면 박스 상하로 분리

### 4.4 hierarchy level 일관성

같은 계층의 block은 비슷한 크기와 스타일을 사용한다.

예:

* top-level peers는 동일한 큰 박스
* internal memories는 동일 스타일
* external actors는 회색 tone

---

## 5) Labeling Rules

### 5.1 블록 라벨

권장 포맷:

```html
<b>instanceName</b><br/>
<font style="font-size:10px">BlockType</font><br/>
<font style="font-size:10px">role description</font>
```

### 5.2 화살표 라벨

화살표 라벨에는 우선순위대로 아래 정보를 넣는다.

1. interface / bundle / signal group 이름
2. protocol/type
3. 필요하면 핵심 control keyword

예:

* `fetchReq Decoupled`
* `missResp Valid`
* `metaRead req/resp`
* `AXI AW`
* `flush`
* `ready/backpressure`

### 5.3 너무 긴 라벨 금지

아래는 피한다.

* field 나열식 라벨
* 코드 한 줄 복붙
* 장문 설명

필요하면 상세는 별도 note 박스 또는 문서 본문으로 뺀다.

---

## 6) draw.io XML Conventions

### 6.1 기본 셀 구성

모든 `.drawio`는 하나의 `<mxGraphModel>` 아래 `<root>`를 가져야 한다.

최소 구조:

* root cell `0`
* layer cell `1`
* vertex cells for blocks
* edge cells for interfaces

### 6.2 sub-block rectangle style

sub-block에는 `mxCell` vertex를 사용하고, 기본적으로 rounded rectangle 스타일을 사용한다.

권장 예:

```text
rounded=1;whiteSpace=wrap;html=1;strokeWidth=1;
fontSize=12;align=center;verticalAlign=middle;
```

### 6.3 memory block style

memory는 일반 block과 구별되는 스타일을 권장한다.

예:

```text
rounded=0;whiteSpace=wrap;html=1;strokeWidth=1;
fillColor=#fff2cc;
```

### 6.4 edge style

권장 기본:

```text
edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;
```

data/control/backpressure에 따라 아래를 차등 적용한다.

* data: 실선
* control: dashed=1
* backpressure: dashed=1 + muted color

### 6.5 source/target 규칙

모든 edge는 가능하면 명시적 `source` / `target` cell id를 가져야 한다.  
floating edge는 특별한 이유가 없으면 피한다.

### 6.6 value 포맷

#### vertex value

```html
<b>instanceName</b><br/>
<font style="font-size:10px">BlockType</font><br/>
<font style="font-size:10px">role description</font>
```

#### edge value

```text
interfaceName
```

또는

```text
interfaceName protocol
```

---

## 7) Quality Checklist

다이어그램 생성 전/후 아래를 반드시 확인한다.

* [ ] 모든 block이 코드상 instantiate 또는 architecturally meaningful memory인가
* [ ] top-level external I/O peer가 포함되어 있는가
* [ ] 같은 block type 여러 개일 때 instance name이 구분되는가
* [ ] 특정 연결 문법을 기계적으로 양방향으로 해석하지 않았는가
* [ ] data path와 control/backpressure path가 구분되는가
* [ ] request/response/interface가 과도하게 field 단위로 분해되지 않았는가
* [ ] 반대로 중요한 flush/stall/ready가 data path에 묻히지 않았는가
* [ ] memory bank/port 구조가 중요한 경우 diagram에 반영되었는가
* [ ] dominant dataflow 방향이 한눈에 보이는가
* [ ] 선 교차가 과도하지 않은가
* [ ] draw.io XML이 단일 `<mxGraphModel>/<root>` 구조를 만족하는가

---

## 8) 권장 작업 순서

1. 상위 블록에서 instantiated sub-block 목록 수집
2. top-level IO와 외부 peer 수집
3. sub-block 간 연결과 port direction 추적
4. data/control/backpressure로 인터페이스 분류
5. memory 구조와 queue/buffer 포함 여부 결정
6. rough block placement
7. draw.io XML 작성
8. checklist 검증

---

### END
