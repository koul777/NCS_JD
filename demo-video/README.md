# NCS JD Remotion demo

NCS JD의 공고문 입력 → NCS 근거 매칭 → 검토용 HWPX 생성 흐름을 보여주는 50초 자막형 제품 데모다. 배경음은 외부 음원 없이 `scripts/generate-audio.mjs`가 결정적으로 생성한다.

## 미리보기

```powershell
npm run studio
```

## 전체 영상과 README 갤러리 렌더링

```powershell
npm run render
npm run still
npm run render:clips
npm run render:previews
```

생성 파일:

- `reports/demo/ncs-jd-demo.mp4`
- `reports/demo/ncs-jd-demo-cover.png`
- `reports/demo/ncs-jd-input.mp4` / `.gif`
- `reports/demo/ncs-jd-evidence.mp4` / `.gif`
- `reports/demo/ncs-jd-result.mp4` / `.gif`

README에는 움직이는 GIF를 직접 표시하고, 각 GIF를 고화질 MP4에 연결한다. 최종 미디어와 `src/`, 설정, 오디오 생성 스크립트는 Git으로 추적한다. 프레임 QA용 `out/`과 재생 가능한 `public/demo-bed.wav`는 Git에서 제외한다.
