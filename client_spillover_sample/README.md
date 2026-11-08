# GPT image 클라이언트 Spillover 샘플

[검증 방안](../docs/gpt-image-client-spillover-validation.md)에 맞춰 **Base HTTP 429 → 지정한 PAYG 배포로 한 번 전환**하는 이미지 편집 샘플입니다.

- `BASE_ENDPOINT` / `SPILLOVER_ENDPOINT`: 각 리소스의 `/openai/v1/` endpoint. 같은 리소스이면 같은 URL 사용 가능
- `BASE_DEPLOYMENT` / `SPILLOVER_DEPLOYMENT`: 각 리소스의 배포 이름
- SDK 자동 재시도 없음(`max_retries=0`), 429 외 오류·연결 실패·타임아웃은 전환하지 않음
- Spillover도 실패하면 오류 반환. 한 작업에서 최대 두 번 호출
- 전환 시 이미지 파일을 다시 열어 같은 이미지·프롬프트를 전송

같은 endpoint를 사용하면 배포 이름을 서로 다르게 지정합니다. 동일 endpoint·동일 배포로의 전환은 막습니다.

## 준비

Python 3.10 이상과 Azure CLI를 사용합니다. 저장소 루트에서 실행하세요.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

두 대상에 동일 모델·버전을 배포하고 인증·호출 권한을 확인합니다. 검증용 Base PAYG는 **1 RPM**으로 설정하고, Spillover PAYG는 테스트 트래픽을 받을 여유 용량을 확보합니다. 서버 측 Spillover는 설정하지 않습니다.

```bash
export BASE_ENDPOINT="https://<base-resource>.cognitiveservices.azure.com/openai/v1/"
export SPILLOVER_ENDPOINT="https://<spillover-resource>.cognitiveservices.azure.com/openai/v1/"
export BASE_DEPLOYMENT="gpt-image-base"
export SPILLOVER_DEPLOYMENT="gpt-image-spillover"
az login
```

기본 인증은 Azure CLI 토큰입니다. API key를 사용한다면 각 리소스의 키를 `BASE_AZURE_OPENAI_API_KEY`와 `SPILLOVER_AZURE_OPENAI_API_KEY` 환경변수에 각각 설정합니다. 키를 코드에 넣지 않습니다.

## 로컬 분기 테스트

```bash
python -m unittest discover -s client_spillover_sample -p test_client_spillover.py -v
```

실제 Azure 호출 없이 Mock으로 확인합니다.

| Base 결과 | 기대 동작 |
|---|---|
| HTTP 200 | Base 결과 반환, Spillover 호출 없음 |
| HTTP 429 (`error.code=null` 또는 `rate_limit_exceeded`) | Spillover 한 번 호출 |
| HTTP 400/401/403/404/500/503 | 오류 반환, 전환 없음 |
| 연결 실패 / 타임아웃 | 오류 반환, 전환 없음 |
| Base 429 → Spillover 429/500 | 오류 반환, 세 번째 호출 없음 |

서로 다른 리소스와 동일 리소스의 별도 배포 모두에서 전환, 인증, 이미지 전체 재업로드를 검증합니다.

## 단일 요청

사용할 이미지 `input.jpg`와 UTF-8 프롬프트 파일 `prompt.txt`를 직접 준비합니다. 파일 경로는 필수입니다.

```bash
python client_spillover_sample/client_spillover.py \
  --image ./input.jpg --prompt ./prompt.txt \
  --output output/client_spillover/single.jpg
```

각 리소스를 직접 호출해 인증 오류를 먼저 해결하세요. Spillover를 직접 점검하려면 두 endpoint·배포·API key의 역할을 함께 교환하고, 점검 후 원래 설정으로 복구합니다.

## 제한된 실제 테스트

Base의 실제 한도가 1 RPM인지 확인한 뒤 **10초 간격으로 최대 6개 작업**을 시작합니다. 이미지 응답이 오래 걸려도 다음 요청은 일정 간격으로 시작합니다.

```bash
mkdir -p output/client_spillover/live-test
pids=()
for i in $(seq 1 6); do
  python client_spillover_sample/client_spillover.py \
    --image ./input.jpg --prompt ./prompt.txt \
    --output "output/client_spillover/live-test/request_${i}.jpg" \
    >"output/client_spillover/live-test/request_${i}.log" 2>&1 &
  pids+=("$!")
  if [ "$i" -lt 6 ]; then sleep 10; fi
done

failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=$((failed + 1))
done
echo "failed jobs: $failed"
grep -H -E 'attempt route=|response route=|client_spillover|completed route=|Request failed' \
  output/client_spillover/live-test/request_*.log
```

동일 작업의 로그에서 **Base 429 → Spillover 배포 호출 → Spillover 200 → 출력 이미지 저장**을 확인합니다. 성공 로그의 예시는 [검증 방안](../docs/gpt-image-client-spillover-validation.md#성공-로그-예시)을 참고하세요.

특정 요청에서 반드시 429가 나온다고 가정하지 않습니다. 429가 없으면 전환 검증은 미완료입니다. 프로세스·인증 지연으로 실제 도착 간격도 달라질 수 있습니다. 운영 트래픽과 분리하고, 추가 부하는 한도를 정한 후 실행하세요.

이 테스트는 클라이언트의 429 감지·요청 전환을 검증하며, PTU 처리량을 검증하는 것은 아닙니다. 서버의 `x-ms-is-spilled-over` 헤더 대신 클라이언트의 실제 호출 endpoint와 최종 결과로 판정합니다.

## 실제 검증 결과 (2026-11-08)

West US 3의 같은 리소스에서 다음 두 PAYG 배포로 검증했습니다. 두 배포 모두 1 RPM, 모델 `gpt-image-2.5-flare` / 버전 `2026-09-08`입니다.

- Base: `gpt-image-2.5-flare-base`
- Spillover: `gpt-image-2-flare`

| 경로 | 작업 수 | 결과 |
|---|---:|---|
| Base 200 | 2 | 정상 이미지 반환, Spillover 호출 없음 |
| Base 429 → Spillover 200 | 2 | 정상 이미지 반환, 작업당 두 번 호출 |

총 4개 작업·6회 HTTP 호출, 실패 0건, 자동 재시도 0회였습니다. 최초 전환 성공 후 추가 작업 시작을 중단하고 진행 중인 작업만 완료했습니다. 출력 JPEG 4개가 모두 1152×864인지 확인했습니다. 이 결과는 해당 실행의 관측이며, 특정 번째 요청에서 항상 429가 발생한다는 의미는 아닙니다.
