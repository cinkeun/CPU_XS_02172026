# 2ahead_taken_branch_analysis.md

## Scope
- Source: `248208.237169.pdf` ("Multiple-Block Ahead Branch Predictors")
- This note summarizes only the mechanism described in the paper.

## 1) 핵심 아이디어: Two-Block Ahead Predictor
논문의 핵심은 "현재 block 정보로 다음 block이 아니라, **다다음 block**을 예측"하는 것이다.

- 기존(one-block ahead): 현재 block -> 다음 block 예측
- 제안(two-block ahead): 현재 block -> 다다음 block 예측

논문은 이를 아래 3개 구조로 구성한다.
- Two-block ahead Prediction Table (PT)
- Two-block ahead Branch Target Buffer (BTB)
- Two-block ahead Return stack 구조 (RAS + SAS)

(논문 Section 4, p.119~122)

### 1.1 2-ahead approach의 두 가지 장점 (논문 기준 정리)
1. Higher fetching bandwidth (double I-fetch)
- 한 cycle에 2개 fetch block(A, B)을 사용해 다음 cycle의 2개 block(C, D)을 예측/fetch 가능
- 전제: I-cache, PT, BTB가 dual-ported 또는 interleaved 구조

2. Branch prediction/address generation 파이프라이닝 (single I-fetch)
- 목표는 "2 blocks/2 cycles fetch"가 아니라, single I-fetch에서 주소생성/예측 경로를 2-stage로 파이프라인
- 효과: 고클럭 설계 가능, 또는 더 큰 BTB/PT 사용 가능 (time budget 확보)
- interleaved 접근은 double I-fetch 구현 시 A/B 동시 처리 맥락에서 유효

## 2) 동작 방식 (Double I-fetch 기준)
논문 Figure 3/4 기준으로, 한 cycle에 A, B 두 block을 fetch하면:
- `Aa` 정보를 사용해 `Ci`를 예측
- `Bb` 정보를 사용해 `Di`를 예측

즉,
- `Aa -> Bi`
- `Bb -> Ci`
- `Cc -> Di`
형태의 연결을 이용해 2개 후속 block 주소를 한 cycle에 계산한다.

### 실제 동작 예시 1 (Figure 3 개념 그대로)
가정:
- 현재 cycle fetch block: `A`, `B`
- `Aa`는 A의 마지막 branch, `Bb`는 B의 branch

계산:
1. `Aa` 관련 BTB/PT 정보를 읽어 `Ci` 계산
2. 동시에 B 주소로 BTB 접근을 시작
3. `Ci` 계산 중 생성된 정보(예: transition type)와 B 관련 BTB 결과를 이용해 `Di` 계산

결과:
- 다음 cycle에서 `C`, `D`를 fetch 가능

## 3) Dual-ported BTB는 어떻게 쓰이고, 왜 필요한가
논문은 double I-fetch에서 두 주소(A와 B)를 같은 cycle에 다뤄야 하므로, BTB/PT가 다음 중 하나여야 한다고 명시한다.
- fully dual-ported
- interleaved

(논문 Section 4.5, p.122)

### 사용 방식
- 같은 cycle에 `A`와 `B`로 BTB 접근
- `AaX` 엔트리로 `Ci` 계산
- 이어서 `BbY` 엔트리 tag check를 진행해 `Di` 계산

(논문 Section 4.2, p.120~121)

### 왜 꼭 필요한가
dual-port(or interleave)가 없으면, A/B 중 하나를 다음 cycle로 미뤄야 해서:
- 2-block 동시 예측 목표가 깨지고
- fetch bandwidth 향상 효과가 줄며
- 논문이 노리는 "한 cycle에 2개 후속 block 주소 예측"이 성립하지 않는다.

즉, two-block ahead predictor의 핵심 성능 포인트를 유지하려면 BTB/PT의 동시 다중 접근 능력이 필수다.

## 4) BTB 구조와 조회 규칙
논문 BTB 엔트리는 일반 BTB와 달리 "Bb 기준"이 아니라 "이전 branch `Aa` + 전이 타입" 기준으로 매핑된다.

- 저장 내용: target, branch type, branch position(b in block B)
- 인덱싱 키: 이전 branch 주소 + transition type (`Aa->Bi`)
- 전이 타입: T / N / R

(논문 Section 4.2, p.120~121)

또한 `AaX` miss 시에는,
- B에 branch가 없다고 가정하고
- `b`를 line 마지막 instruction으로 두며
- transition `Y`를 fall-through로 가정해 `Di` 계산을 진행한다.

(논문 Section 4.2, p.121)

## 5) Training(Update) 방식

### 5.1 BTB training (핵심)
논문은 다음 상황에서 BTB 엔트리를 할당/갱신한다고 설명한다.
- taken branch `Bb`가 mispredicted 또는 misfetched일 때
- 해당 branch의 target/type/position을 BTB에 기록

(논문 Section 4.2, p.120)

또한 two-block ahead BTB는 `AaT`, `AaN`처럼 같은 `Aa`에 대해 복수 엔트리가 생길 수 있다.
- 이는 predecessor가 여러 개인 branch에서 중복을 만들 수 있지만,
- 시뮬레이션상 추가 엔트리 요구는 크지 않았다고 보고한다.

(논문 Section 4.2, p.121, Section 6.2 p.126)

### 5.2 Return 관련 training (PpR + SAS)
반환(return) 경로는 일반 BTB만으로는 예측이 어렵다고 보고, 논문은 SAS를 추가한다.

- branch in block C가 mispredicted일 때, `BbT` 대신 `PpR` 엔트리 할당
- call `Pp` fetch 시 BTB의 `PpR`를 찾아 SAS에 push
- return pop 시 SAS도 pop하여 "return target 다음 block" 예측에 사용

(논문 Section 4.3, p.121~122, Figure 5)

### 실제 동작 예시 2 (return case)
1. call `Pp`를 fetch하면 `PpR` 정보를 SAS에 push
2. 이후 return branch를 만나 RAS로 `Ci`를 얻음
3. 동시에 SAS pop 값으로 `Di`(return 다음 block) 예측

이 방식으로 return target 변동성이 큰 경우에도 후속 block 예측을 안정화한다.

### 5.3 PT training (논문 원리 기반)
논문은 PT 자체는 gshare/gselect 같은 기존 기법을 two-block ahead indexing으로 적응 가능하다고 설명한다.

- 예측 시: `(Aa, Ha)`를 사용해 `Ci` 방향 예측
- 따라서 update도 동일 원리로, 실제 결과가 확정되면 대응되는 two-block ahead 문맥(`Aa, Ha`)에 반영하는 구조로 해석된다.

(논문 Section 4.1, p.120; Section 6.1, p.125)

### 5.4 Training pseudocode (A->B->C 시점 기준)
아래 pseudocode는 논문 설명(Section 4.1/4.2/4.3)을 구현 관점으로 정리한 것이다.

```text
// -----------------------------
// Predict path (fetch time)
// -----------------------------
onFetch(A, B):
  X = transitionType(Aa -> B)              // X in {T, N, R}
  predC = predictWithAaX(Aa, X, Ha)        // PT + BTB + (RAS/SAS if needed)
  Y = transitionType(Bb -> predC)          // predC로부터 파생된 전이 타입
  predD = predictWithBbY(Bb, Y, Hb)

  // 나중 update를 위해 two-block-ahead context를 저장
  metaQ.push({
    Aa, Ha, X,                              // C 예측에 사용된 컨텍스트
    Bb, Hb, Y,                              // D 예측에 사용된 컨텍스트
    predC, predD
  })

// -----------------------------
// Resolve/Train path
// -----------------------------
onResolve(branch Bb_actual):
  m = findMetaFor(Bb_actual)                // Bb와 연관된 oldest 유효 메타

  // (1) PT update: key는 (Aa, Ha), label은 Bb_actual outcome
  // 즉 A->B->C가 확정되는 시점에 C를 결정한 branch(Bb)의 실제 결과로 학습
  PT.update(index=(m.Aa, m.Ha), outcome=Bb_actual.taken)

  // (2) BTB update (논문 기준): taken branch가 mispred/misfetch면 기록
  if Bb_actual.taken and (mispredict(Bb_actual) or misfetch(Bb_actual)):
    BTB.update(
      key=(m.Aa, m.X),                      // AaT / AaN / AaR
      target=Bb_actual.target,              // => C
      type=Bb_actual.type,
      position=Bb_actual.positionInBlockB
    )

// -----------------------------
// Flush rule (speculation recovery)
// -----------------------------
onFlush(flushPoint):
  // flushPoint보다 younger meta만 제거
  // flush 원인을 만든 branch 및 그 branch까지의 older meta는 유지되어
  // resolve 시 training 가능
  metaQ.discardYoungerThan(flushPoint)
```

위 흐름의 핵심은:
- `A->B`만으로는 학습하지 않고, `Bb` 실제 결과가 나온 뒤(`A->B->C` 확정 시점) 학습한다.
- flush가 발생해도 원인 branch(`Bb`)와 older 메타는 살아 있으므로 update가 가능하다.

## 6) 성능 관찰 (논문 보고)

- PT 정확도: two-block ahead와 one-block ahead의 misprediction rate 차이가 매우 작음
  - 개별 벤치 차이가 0.30% 이내로 보고됨
  - (Section 6.1, p.125)

- BTB hit rate/구조:
  - two-block ahead BTB는 set-associative가 필요
  - 최대 hit rate 도달에 대체로 4-way associativity 수준
  - one-block 대비 hit rate 열세는 대체로 0.5% 미만으로 보고
  - (Section 6.2, p.126)

## 7) 요약
- Two-block ahead predictor의 장점은 (1) double I-fetch에서 2 blocks/cycle 예측/fetch, (2) single I-fetch에서 2-stage branch prediction/address generation 파이프라이닝이다.
- 이를 위해 BTB/PT의 동시 다중 접근(dual-port/interleave)이 구조적으로 필수다.
- Training은 BTB 측면에서 mispredict/misfetch 기반 업데이트가 명확하며, return은 `PpR + SAS`로 별도 처리한다.
- 논문 결과상, 정확도 손실은 매우 작고(PT 기준), BTB 구조 비용도 관리 가능한 범위로 제시된다.
