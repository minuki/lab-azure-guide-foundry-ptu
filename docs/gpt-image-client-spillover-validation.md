# GPT image 모델 - Client Spillover 검증 방안

## 검증 목적

**Base endpoint에서 HTTP 429를 받으면, 클라이언트가 같은 요청을 Spillover endpoint로 전환하는지 확인합니다.**

실제 운영에서는 **PTU를 Base endpoint로 주로 사용하고, 요청이 넘치면 PAYG로 Spillover**하는 구성을 사용할 수 있습니다.

다만 **클라이언트의 전환 로직 자체를 검증할 때는 PTU를 사용할 필요가 없습니다.** 전환 조건이 배포 유형(PTU/PAYG)이 아닌 **HTTP 상태 코드 429**라면, **1 RPM(분당 1회)으로 설정한 테스트용 PAYG를 Base endpoint로 사용해도 429 감지와 PAYG Spillover endpoint로의 요청 전환을 검증하기에 충분합니다.** 테스트를 위해 PTU에 큰 부하를 줄 필요가 없습니다.

즉, 운영 구성은 **PTU → PAYG**, 로직 검증 구성은 **저 RPM PAYG → PAYG**입니다. 이는 클라이언트 전환 로직의 검증이며, 실제 PTU의 처리량이나 용량 초과 동작을 검증하는 것은 아닙니다.

## 테스트 구성

| 역할 | 구성 |
|---|---|
| Base endpoint | **1 RPM(분당 1회)으로 배포한 테스트용 PAYG** |
| Spillover endpoint | 다른 리소스의 동일 모델·버전 배포. 테스트 요청을 처리할 여유 용량 확보 |

코드에 두 endpoint와 각 배포 이름을 별도로 지정합니다. 인증 정보도 각 리소스에 맞게 설정합니다.

```text
Base 성공     → 결과 반환
Base HTTP 429 → Spillover로 같은 요청을 한 번 전송
Base 기타 오류 → 오류 반환, 전환하지 않음
```

## 참고: PTU/PAYG 응답 본문의 차이

실제 이미지 편집 테스트에서는 둘 다 **HTTP 429**를 반환했지만, 응답 본문은 다음과 달랐습니다.

### PTU 응답 원문 (HTTP 429)

```json
{
  "error": {
    "message": "Rate limit exceeded.",
    "type": "invalid_request_error",
    "param": null,
    "code": null
  }
}
```

### PAYG 응답 원문 (HTTP 429)

```json
{
  "error": {
    "message": "Your requests to gpt-image-2.5-flare for gpt-image-2-flare in westus3 have exceeded request rate limit.",
    "type": "too_many_requests",
    "param": null,
    "code": "rate_limit_exceeded"
  }
}
```

따라서 `error.code`나 메시지 문자열이 아닌 **HTTP 상태 코드 429로 전환을 판단**해야 합니다. 위 값은 이번 테스트의 관측 결과이며, 모든 배포에서 동일하다고 가정하지 않습니다.

**이 방법은 클라이언트의 전환 로직을 검증합니다. 실제 PTU 처리량·용량 초과 특성·재시도 대기 시간은 별도 검증이 필요합니다.**

## 샘플 코드

[샘플 코드](../client_spillover_sample/client_spillover.py)는 다음 설정을 각각 지정합니다.

- `BASE_ENDPOINT`: 1 RPM 테스트용 PAYG endpoint
- `SPILLOVER_ENDPOINT`: 여유 용량이 있는 PAYG endpoint
- `BASE_DEPLOYMENT` / `SPILLOVER_DEPLOYMENT`: 각 endpoint의 배포 이름

샘플은 **HTTP 429일 때만 한 번 전환**하며, SDK 자동 재시도를 사용하지 않습니다. 인증 정보는 각 리소스에 맞게 설정합니다. 실행 방법은 [상세 테스트 가이드](../client_spillover_sample/README.md)를 참고하세요.

## 검증 절차

실제 부하를 주기 전에 로컬 Mock 테스트로 PTU/PAYG의 두 형태의 429 응답 모두에서 전환이 동작하는지 확인할 수 있습니다.

1. 두 endpoint를 각각 호출해 인증·배포·요청 형식이 정상인지 확인합니다.
2. SDK 자동 재시도를 끕니다(`max_retries=0`). 서버 측 spillover도 설정하지 않아 클라이언트의 전환을 직접 확인합니다.
3. Base의 실제 한도가 **1 RPM**인지 확인하고, 이를 초과하는 속도로 **횟수를 제한해** 요청합니다. 긴 이미지 처리 시간에 요청 속도가 낮아지지 않도록 응답을 기다리지 않고 일정 간격으로 요청을 시작합니다.
4. 같은 작업의 로그에서 **Base HTTP 429 → Spillover 호출 → Spillover 성공** 순서와 실제 호출 endpoint를 확인합니다.

Base를 **1 RPM**으로 배포한 뒤, 10초 간격으로 최대 6회 요청하는 방식으로 시작할 수 있습니다. 한도를 넘었다고 특정 요청에서 반드시 429가 발생하는 것은 아니므로, **실제 429가 관측됐는지** 확인해야 합니다. 429가 없으면 전환 검증은 미완료입니다.

> 운영 배포의 RPM을 낮추지 말고 테스트용 배포를 사용하세요.

## 성공 기준

| 상황 | 기대 동작 |
|---|---|
| Base 성공 | Spillover를 호출하지 않음 |
| Base HTTP 429 | 다른 endpoint로 동일 요청을 한 번 전송하고 성공 결과 반환 |
| Base의 429 외 오류 | 전환하지 않고 오류 반환 |
| Spillover도 실패 | 오류 반환. 무한 재시도나 Base로 재전환하지 않음 |

### 성공 로그 예시

같은 작업의 로그에서 다음 순서를 확인합니다. endpoint·배포 이름 등은 예시입니다.

```text
attempt route=base endpoint=https://<base-resource>/openai/v1/ deployment=...
response route=base HTTP=429 ...
client_spillover base_HTTP=429 -> route=spillover; no base retry
attempt route=spillover endpoint=https://<spillover-resource>/openai/v1/ deployment=...
response route=spillover HTTP=200 ...
completed route=spillover output=... bytes=...
```

**서로 다른 endpoint를 호출했는지, 최종 결과가 반환됐는지, 한 작업의 API 호출이 최대 두 번인지** 확인합니다.

클라이언트 Spillover는 애플리케이션이 다른 endpoint로 요청을 다시 보내는 방식입니다. `x-ms-is-spilled-over`는 서버 내부 전환을 나타내므로, 이 값이 `false`여도 클라이언트 전환은 성공할 수 있습니다. 성공 여부는 **클라이언트의 호출 로그와 최종 결과**로 판단합니다.
