# pipeline_rule.md

> 최우선 원칙: 모든 분석은 반드시 **code-based only**로 수행한다.  
> `web-search` 결과나 기억 기반 정보에 의존한 추론/서술은 금지한다.

> 목적: 단일 모듈 내부의 파이프라인 구조를 코드에서 추출하여, **stage 경계 / stage별 behavior / 제어 흐름(stall·flush·valid)**을 draw.io pipeline diagram으로 일관되게 표현한다.  
> 모듈 내부에서 발생하는 모든 behavior는 이 규칙에 따라 pipeline 형식으로 표현 가능해야 한다.

---

## 0) 기본 원칙

### 0.1 적용 범위

이 규칙은 특정 언어 전용이 아니다. 아래 입력을 모두 포함한다.

* Verilog / SystemVerilog
* VHDL
* Chisel / Scala RTL
* 그 외 구조적 HW 기술 언어

### 0.2 "신뢰 가능한 근거"로 삼을 것

다이어그램에 들어가는 모든 stage / behavior / 제어 경로는 아래 코드 단서 중 하나 이상으로 근거를 가져야 한다.

#### Stage 경계 근거

명시적 레지스터 또는 queue를 stage 경계로 본다.

| 언어 | 예시 |
| --- | --- |
| Verilog / SystemVerilog | `always_ff`, pipeline register, `fifo` instance, named stage signal |
| VHDL | clocked process, register signal, enumerated state boundary |
| Chisel / Scala | `RegNext`, `RegEnable`, `RegInit`, `ShiftRegister`, `Pipe(...)`, `Queue(...)` |

공통 naming 단서: `s0/s1/s2`, `stage0/stage1`, `pipe0/pipe1`, `r_xxx_q`, `xxx_reg`

#### Stage 내 Behavior 근거

stage 경계 사이에서 발생하는 **combinational logic**을 behavior로 본다.

* lookup / access: memory read, CAM lookup, TLB access
* compute: adder, comparator, encoder, decoder, mux
* check: valid check, tag compare, permission check, error detect
* decision: hit/miss determination, branch prediction, arbitration
* generate: request generation, address calculation, data forwarding

#### 제어 경로 근거

| 종류 | 단서 |
| --- | --- |
| Stall (upstream hold) | `ready`, `stall`, `hold`, `!out_ready`, credit depletion |
| Flush / Kill | `flush`, `kill`, `redirect`, `invalidate`, `squash` |
| Valid propagation | `valid`, `en`, `fire`, `io.out.valid :=`, pipeline enable signal |
| Replay | `replay`, `refill`, `retry` |
| Error | `ecc_err`, `bus_err`, `exception`, `pf`, `af` |

### 0.3 stage를 임의로 만들지 않는다

아래 중 하나에 해당해야만 별도 stage 박스로 표현한다.

* 코드에 명시적 register boundary가 있음
* 코드에 stage naming이 있음
* 두 구간 사이에 queue/buffer가 있음
* stage 구분이 모듈 이해에 본질적으로 필요

그 외 단순 combinational chain은 같은 stage 내 behavior로 기술한다.

### 0.4 non-pipeline behavior도 pipeline으로 표현

FSM, arbiter, one-shot logic처럼 전통적 pipeline이 아닌 경우에도 아래 방식으로 pipeline 형식에 맞춘다.

* **FSM**: 각 state를 stage로 표현하고, state transition을 stage 간 화살표로 표현
* **Multi-cycle operation**: cycle마다 발생하는 동작을 stage로 표현
* **Single-cycle module**: 하나의 stage(`S0`)에 모든 behavior를 기술
* **Arbiter**: 각 priority 판단 단계를 stage로 표현

---

## 1) Output Format

### 1.1 산출물 형식

* **File format**: draw.io (`.drawio` XML)
* **Diagram type**: pipeline diagram (left-to-right 또는 top-to-bottom)
* **One page by default**
* 복잡도가 높으면 page를 나누되, 권장 순서:
  * Page 1: pipeline overview (stage + behavior 요약)
  * Page 2: control path detail (stall·flush·valid propagation)

### 1.2 다이어그램 제목

다이어그램 제목은 **분석 대상 모듈의 정확한 이름**을 사용한다.

예: `ICacheMainPipe`, `BPUStage1`, `LoadQueue`

---

## 2) Diagram Structure

### 2.1 전체 레이아웃

```
┌─────────────────────────────────────────────────────────────────┐
│  [External Input]  →  S0  ║  S1  ║  S2  ║ ... ║ Sn  →  [External Output]  │
│                       │        │       │            │                       │
│                   behavior  behavior  behavior  behavior                    │
│                                                                             │
│  ←─────────────── stall / flush / valid (backward) ──────────────────────  │
└─────────────────────────────────────────────────────────────────┘
```

* 파이프라인은 **좌→우** 방향을 기본으로 한다
* 각 stage는 **stage box**로 표현
* stage 사이에 **register boundary marker**를 삽입
* stage 내부에 **behavior list**를 기술
* 데이터/요청 흐름은 **좌→우 실선 화살표**
* stall·flush·valid는 **우→좌 또는 역방향 점선 화살표**
* 외부 입력/출력은 **external box**로 표현

### 2.2 Stage Box

각 stage box에는 아래를 포함한다.

```
┌────────────────────────────┐
│  S0  (stage name/index)    │  ← header: stage 이름
├────────────────────────────┤
│  • behavior 1              │  ← bullet: 이 stage에서 수행하는 동작
│  • behavior 2              │
│  • behavior 3              │
├────────────────────────────┤
│  valid: <condition>        │  ← footer: valid 조건 (코드 근거)
└────────────────────────────┘
```

**Stage 이름 규칙**:

* 코드에 명시적 이름(`s0`, `stage1`, `pipe0`)이 있으면 그대로 사용
* 없으면 `S0`, `S1`, ... 순서로 부여

**Behavior 기술 규칙**:

* 한 줄에 하나의 동작
* 명사구 또는 동사구로 간결하게 (≤ 8 words)
* 코드 signal 이름이나 모듈 이름을 그대로 인용 가능
* 예:
  * `ITLB access (virtual → physical)`
  * `tag array read (4-way)`
  * `tag compare & hit/miss decision`
  * `data array read`
  * `exception check (PF/AF)`
  * `redirect check`
  * `output to fetch buffer`

**Valid 조건 규칙**:

* stage가 유효한 데이터를 보유하는 조건을 코드 근거 기반으로 기술
* 예: `valid: s0_fire`, `valid: s1_valid && !flush`, `valid: io.req.valid`
* 조건이 복잡하면 `valid: see note` 후 note box에 상세 기술

### 2.3 Register Boundary Marker

stage 사이에 레지스터 경계를 시각적으로 표시한다.

draw.io에서는 아래 두 방식 중 하나를 사용한다.

**방식 A: 세로선 (권장)**

* 두 stage 사이에 얇은 수직 사각형 셀을 삽입
* 라벨: `FF` 또는 `Reg`
* 스타일: `fillColor=#dae8fc;strokeColor=#6c8ebf;`

**방식 B: 화살표 위 주석**

* stage 간 화살표 위에 `[FF]` 또는 `[Reg]` 라벨 부착

Queue/Buffer가 경계인 경우:

* 별도 박스로 표현하고 depth/type을 라벨에 명시
* 예: `fetchBuffer\nQueue(8)`

### 2.4 Data Flow Arrows

stage 간 데이터 흐름은 실선 화살표로 표현한다.

* 화살표 라벨: 핵심 데이터 bundle/signal 이름 (생략 가능)
* 방향: 좌 → 우 (기본)
* 스타일: solid, `strokeColor=#000000`

여러 경로로 분기하는 경우(hit/miss, error/no-error 등):

* 분기점에 **diamond(rhombus) 노드**를 삽입하여 조건 명시
* 각 분기 화살표에 조건 라벨 부착 (`[hit]`, `[miss]`, `[error]`)

### 2.5 Control / Backward Path

stall·flush·valid 같은 제어 신호는 역방향 점선 화살표로 표현한다.

| 종류 | 화살표 방향 | 스타일 | 색상 |
| --- | --- | --- | --- |
| Stall (upstream hold) | Sn → S(n-1) | dashed | `#6c8ebf` (blue) |
| Flush / Kill | Sn → S(n-1) → ... → S0 | dashed | `#d6b656` (orange) |
| Replay | external → S0 | dashed | `#d6b656` (orange) |
| Error propagation | Sn → output / exception | dashed | `#b85450` (red) |
| Valid propagation | S0 → S1 → ... (forward) | solid, thin | `#6c8ebf` (blue) |

화살표 라벨에는 정확한 signal 이름을 사용한다.

예: `flush`, `s2_redirect`, `io.in.ready`, `stall_s1`

### 2.6 External Input / Output Box

파이프라인 바깥의 소스/싱크를 표현한다.

* 스타일: 회색 rounded box (`fillColor=#f5f5f5;strokeColor=#666666;`)
* 위치: 좌측(입력), 우측(출력)
* 라벨: 인터페이스/모듈 이름

예:

* 입력: `Upstream\nio.req`, `IFU`, `L1D`
* 출력: `Downstream\nio.resp`, `Fetch Buffer`, `Load Queue`

### 2.7 Conditional / Branching Pipeline

hit/miss, error/no-error 같이 경로가 분기하는 경우:

* 분기 diamond를 stage 내 또는 stage 뒤에 삽입
* 각 경로를 별도 화살표로 표현
* 경로 재합류 지점은 merge diamond 또는 merge 표기로 표현
* 예:

```
S1 → [hit/miss?] → [hit] → S2_hit → output
                 → [miss] → S2_miss → miss handler
```

---

## 3) Derivation Rules (Code → Diagram)

| 코드 단서 | 다이어그램 요소 |
| --- | --- |
| 명시적 pipeline register (`RegNext`, `always_ff`, clocked process) | register boundary marker (FF) |
| `Queue(depth)`, `FIFO`, `SkidBuffer` | queue box (별도 박스, depth 명시) |
| stage naming (`s0`, `stage1`, `pipe0`) | stage header name |
| combinational logic 블록 | stage 내 behavior bullet |
| `when(fire)` / `enable` / port assignment | valid condition (footer) |
| `ready`, `stall`, `hold` signal | backward stall arrow |
| `flush`, `kill`, `redirect`, `squash` | backward flush arrow |
| `replay`, `refill` | replay arrow from miss handler |
| `ecc_err`, `bus_err`, `exception` | error path arrow |
| `hit`/`miss` condition | branch diamond |
| external module port connection | external input/output box |

### 3.1 연결 문법을 방향으로 기계적으로 해석하지 않는다

* 언어마다 연결 문법이 다르므로, 포트/인터페이스 direction 정의를 우선한다.
* Chisel `<>`, VHDL `port map`, Verilog named connection은 "연결 근거"일 뿐이며, 방향은 port/interface 정의에서 확인한다.

### 3.2 multi-stage register chain

여러 stage에 걸쳐 동일 신호가 pipeline되는 경우 (`ShiftRegister`, `Pipe`):

* 해당 신호를 각 stage에서 실선으로 통과시키고, 라벨을 첫 번째 stage에만 표기한다.
* 중간 stage에서 사용되지 않으면 dashed pass-through로 처리한다.

---

## 4) Layout Guidelines

### 4.1 기본 방향

* **left-to-right** 기본 (요청이 왼쪽에서 들어와서 오른쪽으로 나감)
* stage 수가 많아 가로가 너무 길어지면 **top-to-bottom** 사용 가능
* 한 다이어그램 안에서 방향을 섞지 않는다

### 4.2 stage box 크기

* 모든 stage box는 같은 **높이**를 유지한다 (behavior 수가 달라도)
* behavior 수가 많은 stage는 box를 세로로 키우되, 가로 폭은 통일
* register boundary marker는 stage box 사이에 얇게 삽입 (stage box보다 폭이 좁게)

### 4.3 control path 배치

* stall·flush 화살표는 stage box **아래쪽**에 배치하여 data path와 구분
* valid 화살표는 stage box **위쪽**에 배치
* data flow는 **중간**에 배치

권장 수직 배치:

```
[valid propagation]     ← 상단
[stage S0 → S1 → S2]   ← 중간 (data path)
[stall / flush]         ← 하단
```

### 4.4 선 교차 최소화

* data path 실선은 곧고 짧게
* control 점선은 stage box 하단 또는 상단 우회 경로 사용
* 교차가 불가피한 경우 control 화살표가 우회

---

## 5) Labeling Rules

### 5.1 Stage Box 라벨

권장 draw.io value 포맷:

```html
<b>S0</b><br/>
<font style="font-size:10px">• behavior 1<br/>• behavior 2<br/>• behavior 3</font><br/>
<hr/>
<font style="font-size:9px;color:#6c8ebf;">valid: s0_fire</font>
```

### 5.2 Register Boundary 라벨

```html
<b>FF</b>
```

또는 queue인 경우:

```html
<b>Q</b><br/>
<font style="font-size:9px">depth=8</font>
```

### 5.3 화살표 라벨

* 데이터 화살표: interface/bundle/signal 이름 (생략 가능)
* 제어 화살표: 정확한 signal 이름 필수

예: `flush`, `s1_redirect`, `io.in.ready`, `stall_s1`, `miss_resp Valid`

### 5.4 너무 긴 라벨 금지

* behavior bullet: ≤ 8 words
* 화살표 라벨: ≤ 4 words
* 상세 설명이 필요하면 note box로 분리

---

## 6) draw.io XML Conventions

### 6.1 기본 셀 구성

모든 `.drawio`는 하나의 `<mxGraphModel>` 아래 `<root>`를 가져야 한다.

최소 구조:
* root cell `0`
* layer cell `1`
* external input/output vertex cells
* stage vertex cells
* register boundary vertex cells
* queue/buffer vertex cells (해당 시)
* data flow edge cells
* control/stall/flush edge cells

### 6.2 Stage Box Style

```text
rounded=1;whiteSpace=wrap;html=1;
strokeWidth=1;strokeColor=#000000;
fillColor=#ffffff;
fontSize=11;align=center;verticalAlign=top;
```

### 6.3 Register Boundary Style

```text
rounded=0;whiteSpace=wrap;html=1;
strokeWidth=1;strokeColor=#6c8ebf;
fillColor=#dae8fc;
fontSize=10;align=center;verticalAlign=middle;
```

* 권장 크기: 너비 20px, 높이 stage box와 동일

### 6.4 Queue / Buffer Style

```text
rounded=0;whiteSpace=wrap;html=1;
strokeWidth=1;strokeColor=#82b366;
fillColor=#d5e8d4;
fontSize=10;align=center;verticalAlign=middle;
```

### 6.5 External Input/Output Style

```text
rounded=1;whiteSpace=wrap;html=1;
strokeWidth=1;strokeColor=#666666;
fillColor=#f5f5f5;fontColor=#333333;
fontSize=11;align=center;
```

### 6.6 Data Flow Edge Style

```text
edgeStyle=orthogonalEdgeStyle;rounded=0;
orthogonalLoop=1;jettySize=auto;html=1;
strokeColor=#000000;strokeWidth=1.5;
exitX=1;exitY=0.5;entryX=0;entryY=0.5;
```

### 6.7 Stall Edge Style

```text
edgeStyle=orthogonalEdgeStyle;rounded=0;
orthogonalLoop=1;jettySize=auto;html=1;
dashed=1;strokeColor=#6c8ebf;strokeWidth=1;
exitX=0;exitY=0.75;entryX=1;entryY=0.75;
```

### 6.8 Flush / Kill Edge Style

```text
edgeStyle=orthogonalEdgeStyle;rounded=0;
orthogonalLoop=1;jettySize=auto;html=1;
dashed=1;strokeColor=#d6b656;strokeWidth=1;
exitX=0;exitY=0.85;entryX=1;entryY=0.85;
```

### 6.9 Error Edge Style

```text
edgeStyle=orthogonalEdgeStyle;rounded=0;
orthogonalLoop=1;jettySize=auto;html=1;
dashed=1;strokeColor=#b85450;strokeWidth=1;
```

### 6.10 Branch Diamond Style

```text
rhombus;whiteSpace=wrap;html=1;
strokeColor=#000000;fillColor=#fff2cc;
fontSize=10;align=center;verticalAlign=middle;
```

### 6.11 source/target 규칙

* 모든 edge는 명시적 `source` / `target` cell id를 가져야 한다
* floating edge는 피한다

---

## 7) Quality Checklist

다이어그램 생성 전/후 아래를 반드시 확인한다.

* [ ] 모든 stage가 코드상 register boundary 또는 queue로 근거가 있는가
* [ ] stage naming이 코드 naming과 일치하는가 (없으면 S0/S1/... 순서 일관성)
* [ ] 각 stage의 behavior가 combinational logic 근거로 도출되었는가
* [ ] valid 조건이 코드 근거 기반인가
* [ ] data path와 control path(stall/flush)가 시각적으로 구분되는가
* [ ] stall/flush 화살표의 방향이 역방향(우→좌)으로 정확한가
* [ ] external input/output이 포함되어 있는가
* [ ] 분기(hit/miss/error)가 있는 경우 diamond가 포함되어 있는가
* [ ] draw.io XML이 단일 `<mxGraphModel>/<root>` 구조를 만족하는가
* [ ] register boundary marker가 stage 사이마다 삽입되어 있는가
* [ ] FSM/arbiter 등 비전형적 모듈도 pipeline 형식으로 표현되었는가

---

## 8) 권장 작업 순서

1. 모듈의 top-level IO 수집 (external input/output 박스 재료)
2. 코드에서 register boundary 단서 수집 → stage 목록 확정
3. 각 stage의 combinational logic 추출 → behavior bullet 작성
4. 각 stage의 valid 조건 추출
5. stall / flush / replay / error 경로 추출
6. hit/miss 등 분기 조건 추출
7. rough layout 결정 (방향, stage 수, control path 위치)
8. draw.io XML 작성
9. checklist 검증

---

## 9) 코드 근거 인용 포맷

다이어그램 산출 시 아래 형식으로 코드 근거를 기록한다 (note box 또는 별도 md로).

```
<filename>: <근거>

예)
ICacheMainPipe.scala: RegNext(s0_valid) → S0/S1 경계
ICacheMainPipe.scala: io.tlb.req → S0 behavior (ITLB access)
ICacheMainPipe.scala: s1_tag_match_vec → S1 behavior (tag compare)
ICacheMainPipe.scala: io.flush → flush arrow S2→S0
ICacheMainPipe.scala: s1_ready → stall arrow S1→S0
```

---

### END
