import type {CSSProperties, ReactNode} from 'react';
import {
  AbsoluteFill,
  Audio,
  Easing,
  interpolate,
  Sequence,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const C = {
  ink: '#17231d',
  muted: '#68756d',
  line: '#dce4de',
  paper: '#ffffff',
  wash: '#f1f5f1',
  green: '#17643d',
  greenDark: '#0e4a2c',
  greenSoft: '#e7f3eb',
  mint: '#a9d6b7',
  lime: '#cce66d',
  amber: '#e6a43b',
  red: '#aa3027',
};

const FONT = '"Malgun Gothic", "Apple SD Gothic Neo", sans-serif';

const clamp = (value: number, min = 0, max = 1) => Math.max(min, Math.min(max, value));

const fadeFor = (frame: number, duration: number) => {
  return interpolate(frame, [0, 18, duration - 18, duration], [0, 1, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
};

const liftFor = (frame: number, delay = 0, distance = 36) => {
  const progress = interpolate(frame, [delay, delay + 22], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  return {
    opacity: progress,
    transform: `translateY(${(1 - progress) * distance}px)`,
  };
};

const base: CSSProperties = {
  fontFamily: FONT,
  color: C.ink,
};

type IconName =
  | 'upload'
  | 'file'
  | 'database'
  | 'sparkles'
  | 'download'
  | 'check'
  | 'link'
  | 'shield'
  | 'arrow'
  | 'cursor';

const Icon = ({name, size = 28, color = 'currentColor'}: {name: IconName; size?: number; color?: string}) => {
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: color,
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };

  if (name === 'upload') {
    return <svg {...common}><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/></svg>;
  }
  if (name === 'file') {
    return <svg {...common}><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5M10 12h5m-5 4h5"/></svg>;
  }
  if (name === 'database') {
    return <svg {...common}><ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></svg>;
  }
  if (name === 'sparkles') {
    return <svg {...common}><path d="M12 3l1.2 3.3L16.5 7.5l-3.3 1.2L12 12l-1.2-3.3-3.3-1.2 3.3-1.2zM18 14l.8 2.2L21 17l-2.2.8L18 20l-.8-2.2L15 17l2.2-.8zM6 13l.7 1.8 1.8.7-1.8.7L6 18l-.7-1.8-1.8-.7 1.8-.7z"/></svg>;
  }
  if (name === 'download') {
    return <svg {...common}><path d="M12 3v12m0 0l-4-4m4 4l4-4M5 19h14"/></svg>;
  }
  if (name === 'check') {
    return <svg {...common}><path d="M5 12.5l4.2 4.2L19 7"/></svg>;
  }
  if (name === 'link') {
    return <svg {...common}><path d="M10 13a4 4 0 0 0 5.7.1l2.2-2.2a4 4 0 1 0-5.7-5.7L11 6.4"/><path d="M14 11a4 4 0 0 0-5.7-.1L6.1 13.1a4 4 0 1 0 5.7 5.7l1.2-1.2"/></svg>;
  }
  if (name === 'shield') {
    return <svg {...common}><path d="M12 3l7 3v5c0 4.7-2.9 8.1-7 10-4.1-1.9-7-5.3-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg>;
  }
  if (name === 'arrow') {
    return <svg {...common}><path d="M5 12h14m-5-5l5 5-5 5"/></svg>;
  }
  return <svg {...common} fill={color} stroke="none"><path d="M4 2l15.5 10.2-7.2 1.2-3.8 6.4z"/><path d="M11.7 13.1l4.7 5.7" stroke="#fff" strokeWidth="1.7"/></svg>;
};

const Backdrop = ({accent = false}: {accent?: boolean}) => {
  return (
    <AbsoluteFill
      style={{
        background: accent
          ? 'radial-gradient(circle at 76% 18%, rgba(204,230,109,.28), transparent 30%), radial-gradient(circle at 15% 82%, rgba(169,214,183,.32), transparent 34%), #f3f7f3'
          : 'radial-gradient(circle at 85% 5%, rgba(169,214,183,.26), transparent 31%), radial-gradient(circle at 8% 92%, rgba(231,243,235,.8), transparent 34%), #f3f6f3',
      }}
    >
      <div
        style={{
          position: 'absolute',
          inset: 0,
          opacity: 0.28,
          backgroundImage:
            'linear-gradient(rgba(23,100,61,.035) 1px, transparent 1px), linear-gradient(90deg, rgba(23,100,61,.035) 1px, transparent 1px)',
          backgroundSize: '56px 56px',
        }}
      />
    </AbsoluteFill>
  );
};

const Brand = ({dark = false}: {dark?: boolean}) => (
  <div style={{display: 'flex', alignItems: 'center', gap: 14, fontSize: 24, fontWeight: 900, letterSpacing: '-0.04em', color: dark ? '#fff' : C.ink}}>
    <span style={{display: 'grid', placeItems: 'center', width: 58, height: 40, borderRadius: 10, background: dark ? '#fff' : C.green, color: dark ? C.green : '#fff', fontSize: 17, letterSpacing: '-0.02em'}}>NCS</span>
    직무기술서
  </div>
);

const Pill = ({children, tone = 'green'}: {children: ReactNode; tone?: 'green' | 'amber' | 'gray' | 'dark'}) => {
  const palette = {
    green: {background: C.greenSoft, color: C.greenDark},
    amber: {background: '#fff3d9', color: '#87540c'},
    gray: {background: '#eef1ef', color: '#5e6a63'},
    dark: {background: C.greenDark, color: '#fff'},
  }[tone];
  return <span style={{...palette, display: 'inline-flex', alignItems: 'center', borderRadius: 999, padding: '9px 14px', fontSize: 18, fontWeight: 800, whiteSpace: 'nowrap'}}>{children}</span>;
};

const BrowserShell = ({children, style}: {children: ReactNode; style?: CSSProperties}) => (
  <div
    style={{
      position: 'absolute',
      left: 150,
      top: 145,
      width: 1620,
      height: 820,
      overflow: 'hidden',
      border: '1px solid rgba(23,35,29,.14)',
      borderRadius: 28,
      background: C.paper,
      boxShadow: '0 42px 90px rgba(35,62,46,.17), 0 8px 24px rgba(35,62,46,.08)',
      ...style,
    }}
  >
    <div style={{height: 58, padding: '0 22px', display: 'flex', alignItems: 'center', borderBottom: `1px solid ${C.line}`, background: '#fbfcfb'}}>
      <div style={{display: 'flex', gap: 9}}>
        {['#ee6a5f', '#e5b947', '#62b66f'].map((color) => <i key={color} style={{width: 12, height: 12, borderRadius: '50%', background: color}} />)}
      </div>
      <div style={{margin: '0 auto', padding: '8px 110px', borderRadius: 9, color: '#829087', background: '#eef2ef', fontSize: 15}}>127.0.0.1:8000</div>
      <div style={{width: 78}} />
    </div>
    <div style={{height: 762, overflow: 'hidden', position: 'relative'}}>{children}</div>
  </div>
);

const SceneHeadline = ({kicker, title, align = 'left'}: {kicker: string; title: string; align?: 'left' | 'center'}) => (
  <div style={{position: 'absolute', left: align === 'left' ? 150 : 0, right: align === 'center' ? 0 : undefined, top: 50, textAlign: align}}>
    <div style={{color: C.green, fontSize: 18, fontWeight: 900, letterSpacing: '.12em', textTransform: 'uppercase'}}>{kicker}</div>
    <div style={{marginTop: 7, fontSize: 31, fontWeight: 900, letterSpacing: '-.045em'}}>{title}</div>
  </div>
);

const CaptionBar = ({children}: {children: ReactNode}) => (
  <div style={{position: 'absolute', left: '50%', bottom: 42, transform: 'translateX(-50%)', minWidth: 670, padding: '15px 28px', borderRadius: 999, background: 'rgba(14,74,44,.94)', boxShadow: '0 14px 38px rgba(14,74,44,.22)', color: '#fff', fontSize: 22, fontWeight: 800, textAlign: 'center'}}>{children}</div>
);

const Cursor = ({x, y, click = 0}: {x: number; y: number; click?: number}) => (
  <div style={{position: 'absolute', left: x, top: y, zIndex: 50, filter: 'drop-shadow(0 4px 4px rgba(0,0,0,.22))'}}>
    {click > 0 ? <span style={{position: 'absolute', left: -17, top: -17, width: 50, height: 50, border: `3px solid ${C.green}`, borderRadius: '50%', opacity: 1 - click, transform: `scale(${.4 + click * 1.2})`}} /> : null}
    <Icon name="cursor" size={34} color={C.ink}/>
  </div>
);

const IntroScene = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const duration = 180;
  const logoScale = spring({frame, fps, config: {damping: 16, stiffness: 120, mass: .8}});
  const line1 = spring({frame: frame - 18, fps, config: {damping: 18, stiffness: 95}});
  const line2 = spring({frame: frame - 29, fps, config: {damping: 18, stiffness: 95}});
  const orbit = interpolate(frame, [0, duration], [0, 24]);
  return (
    <AbsoluteFill style={{...base, opacity: fadeFor(frame, duration), overflow: 'hidden'}}>
      <Backdrop accent />
      <div style={{position: 'absolute', width: 620, height: 620, right: -180, top: -190, border: '1px solid rgba(23,100,61,.13)', borderRadius: '50%', transform: `rotate(${orbit}deg)`}} />
      <div style={{position: 'absolute', width: 360, height: 360, right: 20, top: -60, border: '1px solid rgba(23,100,61,.18)', borderRadius: '50%', transform: `rotate(${-orbit * 1.4}deg)`}} />
      <div style={{position: 'absolute', left: 155, top: 88, transform: `scale(${logoScale})`, transformOrigin: 'left center'}}><Brand /></div>
      <div style={{position: 'absolute', left: 155, top: 280, width: 1250}}>
        <div style={{color: C.green, fontSize: 22, fontWeight: 900, letterSpacing: '.16em'}}>NCS-EVIDENCE JOB PROFILE</div>
        <div style={{marginTop: 30, fontSize: 104, lineHeight: 1.08, fontWeight: 950, letterSpacing: '-.075em'}}>
          <div style={{opacity: clamp(line1), transform: `translateY(${(1 - line1) * 55}px)`}}>공고문 하나로,</div>
          <div style={{opacity: clamp(line2), transform: `translateY(${(1 - line2) * 55}px)`, color: C.green}}>근거 있는 직무기술서.</div>
        </div>
        <div style={{...liftFor(frame, 58, 24), marginTop: 42, display: 'flex', alignItems: 'center', gap: 14, color: C.muted, fontSize: 27, fontWeight: 700}}>
          <Pill tone="dark">50초 제품 시연</Pill>
          <span>공고문 분석부터 검토용 HWPX까지</span>
        </div>
      </div>
      <div style={{...liftFor(frame, 80, 16), position: 'absolute', right: 155, bottom: 88, display: 'flex', gap: 12}}>
        <Pill>NCS 직접 근거</Pill><Pill tone="gray">source_ref 추적</Pill><Pill tone="amber">draft 전용</Pill>
      </div>
    </AbsoluteFill>
  );
};

const UploadCard = ({primary, filename, progress}: {primary?: boolean; filename: string; progress: number}) => (
  <div style={{position: 'relative', minHeight: 205, padding: 28, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', border: primary ? '2px solid #9fc2aa' : '2px dashed #bdc9c0', borderRadius: 20, background: primary ? 'linear-gradient(145deg,#fff 32%,#edf7f0)' : '#fff', boxShadow: progress > .2 && primary ? `0 0 0 ${8 * Math.sin(progress * Math.PI)}px rgba(23,100,61,.11)` : undefined}}>
    <span style={{padding: '6px 10px', borderRadius: 99, color: C.green, background: C.greenSoft, fontSize: 13, fontWeight: 900}}>{primary ? '1' : '선택'}</span>
    <div style={{margin: '18px 0 10px', color: C.green}}><Icon name="upload" size={30}/></div>
    <strong style={{fontSize: 22, letterSpacing: '-.035em'}}>{primary ? '채용 공고문' : 'NCS 직무기술서 양식'}</strong>
    <small style={{marginTop: 7, color: C.muted, fontSize: 14}}>{primary ? 'PDF · HWP · HWPX · DOCX · TXT' : '기관 양식 PDF · HWP · HWPX'}</small>
    <span style={{marginTop: 'auto', color: C.greenDark, fontSize: 14, fontWeight: 800}}>{filename}</span>
  </div>
);

const InputScene = () => {
  const frame = useCurrentFrame();
  const duration = 360;
  const fileVisible = frame >= 78;
  const title = '행정지원 담당자';
  const typedCount = Math.floor(interpolate(frame, [110, 185], [0, title.length], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}));
  const scroll = interpolate(frame, [205, 265], [0, -305], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const clickUpload = frame >= 56 && frame <= 72 ? (frame - 56) / 16 : 0;
  const clickGenerate = frame >= 290 && frame <= 308 ? (frame - 290) / 18 : 0;
  const cursorX = interpolate(frame, [0, 50, 90, 135, 205, 260, 292, 320], [1500, 510, 510, 1040, 1040, 1260, 1260, 1560], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const cursorY = interpolate(frame, [0, 50, 90, 135, 205, 260, 292, 320], [880, 430, 430, 630, 630, 820, 820, 930], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  return (
    <AbsoluteFill style={{...base, opacity: fadeFor(frame, duration)}}>
      <Backdrop />
      <SceneHeadline kicker="01 · INPUT" title="공고문 직무에서 직무기술서를 만듭니다." />
      <BrowserShell>
        <div style={{transform: `translateY(${scroll}px)`, padding: '34px 70px 80px'}}>
          <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
            <Brand />
            <div style={{display: 'flex', alignItems: 'center', gap: 9, color: C.muted, fontSize: 15}}><i style={{width: 9, height: 9, borderRadius: '50%', background: '#33a465', boxShadow: '0 0 0 5px #dff2e7'}}/> 준비됨</div>
          </div>
          <div style={{marginTop: 36}}>
            <div style={{color: C.green, fontSize: 14, fontWeight: 900, letterSpacing: '.12em'}}>공고문 직무를 구조화</div>
            <h1 style={{margin: '10px 0 12px', fontSize: 44, lineHeight: 1.1, letterSpacing: '-.055em'}}>채용 공고의 담당업무를 NCS 직무기술서로 바꿉니다.</h1>
            <p style={{margin: 0, color: C.muted, fontSize: 16}}>직무명·직무수행내용을 추출하고 NCS 세분류·능력단위·KSA 근거를 연결합니다.</p>
          </div>
          <div style={{display: 'grid', gridTemplateColumns: '1.15fr .85fr', gap: 18, marginTop: 26}}>
            <UploadCard primary filename={fileVisible ? '가상기관_행정직_채용공고.pdf' : '파일을 선택하세요'} progress={clickUpload}/>
            <UploadCard filename="가상기관_직무기술서_예시양식.hwpx" progress={0}/>
          </div>
          <div style={{marginTop: 18, padding: '20px 25px', border: `1px solid ${C.line}`, borderRadius: 18, background: '#fff'}}>
            <div style={{color: C.greenDark, fontSize: 14, fontWeight: 900}}>조직 내 직무명</div>
            <div style={{marginTop: 9, height: 48, padding: '11px 14px', border: `1px solid ${frame >= 105 && frame < 195 ? C.green : C.line}`, borderRadius: 10, background: '#fbfdfb', color: C.ink, fontSize: 18}}>
              {title.slice(0, typedCount)}<span style={{opacity: frame % 18 < 9 ? 1 : 0, color: C.green}}>|</span>
            </div>
          </div>
          <div style={{...liftFor(frame, 174, 14), marginTop: 12, padding: '17px 25px', border: `1px solid ${C.line}`, borderRadius: 18, background: '#fff'}}>
            <div style={{color: C.greenDark, fontSize: 14, fontWeight: 900}}>공고문에서 추출한 직무수행내용</div>
            <div style={{marginTop: 11, display: 'flex', flexWrap: 'wrap', gap: 8}}>
              {['공문서 작성 및 관리', '회의 운영 지원', '행정자료 수집·정리'].map((item) => (
                <span key={item} style={{padding: '8px 11px', borderRadius: 9, color: C.greenDark, background: C.greenSoft, fontSize: 14, fontWeight: 800}}>{item}</span>
              ))}
            </div>
          </div>
          <div style={{marginTop: 18, padding: '22px 25px', display: 'grid', gridTemplateColumns: '.8fr 1.2fr', gap: 28, border: `1px solid ${C.line}`, borderRadius: 18, background: '#fff'}}>
            <div>
              <span style={{padding: '6px 10px', borderRadius: 99, color: C.green, background: C.greenSoft, fontSize: 13, fontWeight: 900}}>2</span>
              <h2 style={{margin: '10px 0 5px', fontSize: 22}}>문장 구성 방식</h2>
              <p style={{margin: 0, color: C.muted, fontSize: 14}}>외부 AI 연결 없이 NCS 근거로 조립합니다.</p>
            </div>
            <div style={{padding: 15, display: 'flex', alignItems: 'center', gap: 14, border: `2px solid ${C.green}`, borderRadius: 14, background: 'linear-gradient(135deg,#fff,#f0f8f3)', boxShadow: `0 0 0 3px ${C.greenSoft}`}}>
              <span style={{display: 'grid', placeItems: 'center', width: 24, height: 24, borderRadius: '50%', border: `2px solid ${C.green}`}}><i style={{width: 12, height: 12, borderRadius: '50%', background: C.green}}/></span>
              <div style={{display: 'flex', flexDirection: 'column', gap: 4}}><strong style={{fontSize: 18}}>로컬 NCS 근거 · 결정적 생성</strong><small style={{color: C.green, fontSize: 14, fontWeight: 800}}>로그인·API 키 없이 재현 가능</small></div>
              <span style={{marginLeft: 'auto'}}><Pill>기본값</Pill></span>
            </div>
          </div>
          <div style={{marginTop: 18, height: 74, padding: '0 26px', display: 'flex', alignItems: 'center', gap: 14, borderRadius: 16, color: '#fff', background: C.green, boxShadow: '0 13px 30px rgba(23,100,61,.2)'}}>
            <span style={{display: 'grid', placeItems: 'center', width: 34, height: 34, borderRadius: '50%', background: 'rgba(255,255,255,.16)', fontWeight: 900}}>3</span>
            <strong style={{fontSize: 21}}>NCS 직무기술서 만들기</strong>
            <span style={{marginLeft: 'auto'}}><Icon name="arrow" size={30}/></span>
          </div>
        </div>
      </BrowserShell>
      <Cursor x={cursorX} y={cursorY} click={clickUpload || clickGenerate}/>
      <CaptionBar>{frame < 205 ? '가상 공고문에서 직무명과 직무수행내용을 추출합니다.' : '추출한 업무를 기준으로 NCS 근거를 찾고 직무기술서를 생성합니다.'}</CaptionBar>
    </AbsoluteFill>
  );
};

const PipelineNode = ({icon, title, subtitle, active, done, delay}: {icon: IconName; title: string; subtitle: string; active: boolean; done: boolean; delay: number}) => {
  const frame = useCurrentFrame();
  const entrance = liftFor(frame, delay, 24);
  return (
    <div style={{...entrance, position: 'relative', padding: '28px 24px', border: `2px solid ${active || done ? C.green : C.line}`, borderRadius: 20, background: active ? '#f2faf4' : '#fff', boxShadow: active ? '0 18px 38px rgba(23,100,61,.12)' : '0 8px 22px rgba(25,50,35,.05)'}}>
      <div style={{width: 54, height: 54, display: 'grid', placeItems: 'center', borderRadius: 15, color: active || done ? '#fff' : C.green, background: active || done ? C.green : C.greenSoft}}>
        {done ? <Icon name="check" size={30}/> : <Icon name={icon} size={29}/>}
      </div>
      <h3 style={{margin: '18px 0 6px', fontSize: 24, letterSpacing: '-.035em'}}>{title}</h3>
      <p style={{margin: 0, color: C.muted, fontSize: 16, lineHeight: 1.5}}>{subtitle}</p>
      {active ? <span style={{position: 'absolute', right: 20, top: 20, width: 12, height: 12, borderRadius: '50%', background: C.lime, boxShadow: '0 0 0 8px rgba(204,230,109,.25)'}}/> : null}
    </div>
  );
};

const PipelineScene = () => {
  const frame = useCurrentFrame();
  const duration = 300;
  const active = Math.min(3, Math.floor(Math.max(0, frame - 32) / 54));
  const bar = interpolate(frame, [25, 245], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const nodes: Array<{icon: IconName; title: string; subtitle: string}> = [
    {icon: 'file', title: 'Kordoc 분석', subtitle: '공고문에서 실제 수행업무를 추출'},
    {icon: 'database', title: 'NCS 매칭', subtitle: '세분류·능력단위·KSA 근거 수집'},
    {icon: 'sparkles', title: '문장 구성', subtitle: '선택된 근거 안에서만 직무 문장 구성'},
    {icon: 'download', title: 'HWPX 생성', subtitle: '검증된 초안을 기관 양식에 반영'},
  ];
  return (
    <AbsoluteFill style={{...base, opacity: fadeFor(frame, duration)}}>
      <Backdrop accent />
      <SceneHeadline kicker="02 · EVIDENCE PIPELINE" title="근거 선택은 먼저, 문장 구성은 나중에." />
      <div style={{position: 'absolute', left: 150, right: 150, top: 220}}>
        <div style={{position: 'relative', height: 7, margin: '0 120px 42px', borderRadius: 99, background: '#dce5de', overflow: 'hidden'}}><div style={{height: '100%', width: `${bar * 100}%`, borderRadius: 99, background: `linear-gradient(90deg,${C.green},${C.lime})`}}/></div>
        <div style={{display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 22}}>
          {nodes.map((node, index) => <PipelineNode key={node.title} {...node} active={index === active} done={index < active} delay={index * 10}/>) }
        </div>
        <div style={{...liftFor(frame, 105, 18), marginTop: 42, padding: '24px 30px', display: 'grid', gridTemplateColumns: 'auto 1fr auto', alignItems: 'center', gap: 20, borderRadius: 18, color: '#fff', background: C.greenDark}}>
          <Icon name="shield" size={34}/>
          <div><strong style={{fontSize: 22}}>결정적 근거 선택</strong><p style={{margin: '5px 0 0', color: '#cfe3d6', fontSize: 16}}>공고문과 NCS에 없는 사실·자격·학력·경력연수는 추가하지 않습니다.</p></div>
          <Pill tone="dark">동일 입력 · 동일 구조</Pill>
        </div>
      </div>
      <CaptionBar>{active < 2 ? 'NCS MCP의 읽기 전용 도구로 필요한 직접 근거를 모읍니다.' : '문장은 확정된 근거 안에서만 구성하며 범위를 바꾸지 않습니다.'}</CaptionBar>
    </AbsoluteFill>
  );
};

const FieldRow = ({label, children, highlight = false}: {label: string; children: ReactNode; highlight?: boolean}) => (
  <div style={{padding: '17px 0', display: 'grid', gridTemplateColumns: '160px 1fr', gap: 22, borderBottom: '1px solid #e7ece8', background: highlight ? 'linear-gradient(90deg,rgba(231,243,235,.82),transparent)' : undefined}}>
    <div style={{color: C.muted, fontSize: 14, fontWeight: 800}}>{label}</div>
    <div style={{fontSize: 16, lineHeight: 1.55, fontWeight: 700}}>{children}</div>
  </div>
);

const SourceChip = ({children}: {children: ReactNode}) => (
  <span style={{marginLeft: 8, padding: '4px 8px', borderRadius: 7, color: C.greenDark, background: C.greenSoft, fontSize: 11, fontWeight: 900, verticalAlign: 'middle'}}>↗ {children}</span>
);

const EvidenceScene = () => {
  const frame = useCurrentFrame();
  const duration = 390;
  const open = spring({frame: frame - 104, fps: 30, config: {damping: 17, stiffness: 110}});
  const tab = frame < 210 ? '직무기술서' : '직무명세서';
  const pageShift = interpolate(frame, [202, 228], [0, -410], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const pulse = Math.sin(frame / 11) * .5 + .5;
  return (
    <AbsoluteFill style={{...base, opacity: fadeFor(frame, duration)}}>
      <Backdrop />
      <SceneHeadline kicker="03 · REVIEW" title="직무기술서와 직무명세서를 나누고, 출처는 연결합니다." />
      <BrowserShell style={{top: 138, height: 825}}>
        <div style={{height: 58, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 28px', borderBottom: `1px solid ${C.line}`, background: '#fff'}}>
          <Brand />
          <div style={{display: 'flex', gap: 9}}><Pill tone="gray">가상 행정직 예시</Pill><Pill tone="amber">DRAFT</Pill><Pill>NCS 원문 연결</Pill></div>
        </div>
        <div style={{height: 704, display: 'grid', gridTemplateColumns: '210px 1fr', background: '#f4f7f4'}}>
          <aside style={{padding: '28px 18px', borderRight: `1px solid ${C.line}`, background: '#fff'}}>
            <div style={{color: C.muted, fontSize: 12, fontWeight: 900, letterSpacing: '.12em'}}>생성된 초안</div>
            {['직무기술서', '직무명세서', '근거 목록', '검토 플래그'].map((item, index) => {
              const selected = item === tab;
              return <div key={item} style={{marginTop: index === 0 ? 18 : 7, padding: '13px 14px', borderRadius: 11, color: selected ? C.greenDark : C.muted, background: selected ? C.greenSoft : 'transparent', fontSize: 15, fontWeight: selected ? 900 : 700}}>{item}{item === '검토 플래그' ? <span style={{float: 'right', color: '#87540c'}}>3</span> : null}</div>;
            })}
          </aside>
          <main style={{position: 'relative', overflow: 'hidden', padding: '28px 330px 40px 36px'}}>
            <div style={{position: 'absolute', left: 36, right: 330, transform: `translateY(${pageShift}px)`}}>
              <div style={{padding: '30px 34px', borderRadius: 18, background: '#fff', boxShadow: '0 8px 28px rgba(36,61,45,.07)'}}>
                <div style={{display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', paddingBottom: 18, borderBottom: `2px solid ${C.greenDark}`}}>
                  <div><div style={{color: C.green, fontSize: 13, fontWeight: 900}}>JOB DESCRIPTION</div><h2 style={{margin: '5px 0 0', fontSize: 28, letterSpacing: '-.04em'}}>행정지원 담당자</h2></div>
                  <Pill>ncs_job_profile_v1</Pill>
                </div>
                <FieldRow label="직무 목적">기관 운영을 위한 종합 행정 업무를 수행합니다.<SourceChip>organization_input</SourceChip></FieldRow>
                <FieldRow label="주요 책무" highlight={frame >= 76 && frame < 195}>문서 작성 · 회의 운영 · 문서 관리<SourceChip>능력단위 3</SourceChip></FieldRow>
                <FieldRow label="핵심 과업">부서 내ㆍ외부에서 요청된 업무 사항을 파악할 수 있다.<SourceChip>수행준거 원문</SourceChip></FieldRow>
                <FieldRow label="조직 입력">공문서 작성 · 회의 운영 지원 · 행정자료 관리 <span style={{marginLeft: 8}}><Pill tone="gray">organization_input</Pill></span></FieldRow>
              </div>
              <div style={{marginTop: 22, padding: '30px 34px', borderRadius: 18, background: '#fff', boxShadow: '0 8px 28px rgba(36,61,45,.07)'}}>
                <div style={{display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', paddingBottom: 18, borderBottom: `2px solid ${C.greenDark}`}}>
                  <div><div style={{color: C.green, fontSize: 13, fontWeight: 900}}>PERSON SPECIFICATION</div><h2 style={{margin: '5px 0 0', fontSize: 28, letterSpacing: '-.04em'}}>직무명세서</h2></div>
                  <Pill tone="amber">필수성 검토 필요</Pill>
                </div>
                <FieldRow label="지식">문서 작성 절차 · 회의 유형·운영 방법<SourceChip>KSA · K</SourceChip></FieldRow>
                <FieldRow label="기술" highlight={frame >= 228}>요구사항 분석 능력 · 회의 운영 계획 능력<SourceChip>KSA · S</SourceChip></FieldRow>
                <FieldRow label="태도">자료의 객관성을 유지하려는 태도<SourceChip>KSA · A</SourceChip></FieldRow>
                <FieldRow label="자격 참고">수집·연결 범위 확인 필요 <span style={{marginLeft: 8}}><Pill tone="amber">reference</Pill></span></FieldRow>
              </div>
            </div>
            <div style={{position: 'absolute', right: 28, top: 28, width: 278, padding: '24px 22px', borderRadius: 18, background: C.greenDark, color: '#fff', transform: `translateX(${(1 - open) * 330}px)`, boxShadow: '0 22px 48px rgba(14,74,44,.24)'}}>
              <div style={{display: 'flex', alignItems: 'center', gap: 10, color: C.lime}}><Icon name="link" size={26}/><strong style={{fontSize: 16}}>SOURCE REF</strong></div>
              <h3 style={{margin: '18px 0 8px', fontSize: 21, lineHeight: 1.35}}>{tab === '직무기술서' ? '부서 내ㆍ외부에서 요청된 업무 사항을 파악할 수 있다.' : '요구사항 분석 능력'}</h3>
              <div style={{marginTop: 19, paddingTop: 18, borderTop: '1px solid rgba(255,255,255,.17)', display: 'grid', gap: 12, color: '#cfe0d4', fontSize: 13}}>
                <div><b style={{color: '#fff'}}>source_type</b><br/>{tab === '직무기술서' ? 'performance_criterion' : 'ksa_item'}</div>
                <div><b style={{color: '#fff'}}>evidence_grade</b><br/>direct</div>
                <div><b style={{color: '#fff'}}>unit_code</b><br/>0202030201_22v3</div>
                <div><b style={{color: '#fff'}}>source_system</b><br/>NCS_MCP</div>
              </div>
              <div style={{marginTop: 18, padding: '12px 14px', borderRadius: 10, background: `rgba(204,230,109,${.12 + pulse * .08})`, color: C.lime, fontSize: 13, fontWeight: 900}}>원문 그대로 보존 · 클릭해 추적</div>
            </div>
          </main>
        </div>
      </BrowserShell>
      <CaptionBar>{frame < 205 ? 'NCS 기반 문장마다 source_ref가 연결되어 원문까지 추적됩니다.' : 'K·S·A는 직무명세서에 분리하고, 필수 여부는 사람이 검토합니다.'}</CaptionBar>
    </AbsoluteFill>
  );
};

const ResultScene = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const duration = 240;
  const pop = spring({frame: frame - 24, fps, config: {damping: 14, stiffness: 120, mass: .8}});
  const filePop = spring({frame: frame - 65, fps, config: {damping: 13, stiffness: 105}});
  const click = frame >= 140 && frame <= 158 ? (frame - 140) / 18 : 0;
  const cursorX = interpolate(frame, [90, 130, 145, 175], [1600, 1450, 1450, 1650], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const cursorY = interpolate(frame, [90, 130, 145, 175], [870, 710, 710, 900], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  return (
    <AbsoluteFill style={{...base, opacity: fadeFor(frame, duration)}}>
      <Backdrop accent />
      <SceneHeadline kicker="04 · EXPORT" title="검증된 HWPX 초안을 바로 내려받습니다." />
      <BrowserShell>
        <div style={{padding: '42px 70px'}}>
          <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}><Brand/><div style={{display: 'flex', alignItems: 'center', gap: 9, color: C.muted, fontSize: 15}}><i style={{width: 9, height: 9, borderRadius: '50%', background: '#33a465'}}/> 준비됨</div></div>
          <div style={{marginTop: 46, padding: '28px 30px', borderRadius: 17, background: '#eaf1ec'}}>
            <div style={{display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 20}}>
              {['Kordoc 공고문 분석','NCS 근거 매칭','결정적 문장 구성','HWPX 생성'].map((item) => <div key={item} style={{display: 'flex', alignItems: 'center', gap: 10, color: C.greenDark, fontSize: 14, fontWeight: 900}}><span style={{width: 22, height: 22, display: 'grid', placeItems: 'center', borderRadius: '50%', color: '#fff', background: C.green}}><Icon name="check" size={15}/></span>{item}</div>)}
            </div>
          </div>
          <div style={{marginTop: 26, padding: '34px 36px', display: 'grid', gridTemplateColumns: 'auto 1fr auto', alignItems: 'center', gap: 24, border: '2px solid #9fc9ac', borderRadius: 22, background: '#fff', boxShadow: '0 22px 55px rgba(23,100,61,.11)', transform: `scale(${.82 + pop * .18})`, opacity: clamp(pop)}}>
            <span style={{width: 64, height: 64, display: 'grid', placeItems: 'center', borderRadius: '50%', color: '#fff', background: C.green}}><Icon name="check" size={36}/></span>
            <div><p style={{margin: 0, color: C.green, fontSize: 14, fontWeight: 900}}>생성 완료</p><h2 style={{margin: '7px 0 9px', fontSize: 27, letterSpacing: '-.04em'}}>행정지원_담당자_직무기술서_초안.hwpx</h2><p style={{margin: 0, color: C.muted, fontSize: 16}}>세분류 <b>1</b>개 · 능력단위 <b>3</b>개 · 외부 AI 미사용</p></div>
            <button style={{padding: '14px 19px', display: 'flex', alignItems: 'center', gap: 9, border: `2px solid ${C.green}`, borderRadius: 11, color: C.green, background: '#fff', fontSize: 16, fontWeight: 900}}><Icon name="download" size={22}/> 다시 다운로드</button>
          </div>
          <div style={{marginTop: 26, display: 'flex', justifyContent: 'center', gap: 12}}><Pill>NCS 직접 근거</Pill><Pill tone="gray">예시 양식 반영</Pill><Pill tone="gray">조직 입력 분리</Pill><Pill tone="amber">검토 플래그 표시</Pill></div>
        </div>
      </BrowserShell>
      <div style={{position: 'absolute', left: 247, top: 690, width: 190, height: 235, padding: '25px 22px', borderRadius: 18, color: C.greenDark, background: '#fff', border: `1px solid ${C.line}`, boxShadow: '0 22px 45px rgba(35,60,43,.14)', opacity: clamp(filePop), transform: `translateY(${(1-filePop)*60}px) rotate(${-5 + filePop * 5}deg)`}}>
        <Icon name="file" size={45}/><div style={{marginTop: 28, fontSize: 20, fontWeight: 950}}>HWPX</div><div style={{marginTop: 6, color: C.muted, fontSize: 13}}>검토용 초안</div><div style={{position: 'absolute', left: 0, right: 0, bottom: 0, height: 12, borderRadius: '0 0 18px 18px', background: C.green}}/>
      </div>
      <Cursor x={cursorX} y={cursorY} click={click}/>
      <CaptionBar>결과는 항상 검토용 draft이며, 채용 결정이나 공식 자격 판정이 아닙니다.</CaptionBar>
    </AbsoluteFill>
  );
};

const OutroScene = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const duration = 180;
  const logo = spring({frame, fps, config: {damping: 17, stiffness: 100}});
  const headline = spring({frame: frame - 12, fps, config: {damping: 18, stiffness: 92}});
  return (
    <AbsoluteFill style={{...base, opacity: interpolate(frame, [0, 18, duration - 12, duration], [0,1,1,1], {extrapolateLeft:'clamp', extrapolateRight:'clamp'}), background: C.greenDark, color: '#fff'}}>
      <div style={{position: 'absolute', inset: 0, opacity: .16, backgroundImage: 'radial-gradient(circle at 75% 20%,#cce66d 0,transparent 30%), radial-gradient(circle at 12% 90%,#a9d6b7 0,transparent 36%)'}}/>
      <div style={{position: 'absolute', left: 160, top: 100, transform: `scale(${logo})`, transformOrigin: 'left center'}}><Brand dark/></div>
      <div style={{position: 'absolute', left: 160, right: 160, top: 302}}>
        <div style={{color: C.lime, fontSize: 21, fontWeight: 900, letterSpacing: '.15em'}}>NCS JD</div>
        <div style={{marginTop: 24, fontSize: 84, lineHeight: 1.14, fontWeight: 950, letterSpacing: '-.065em', opacity: clamp(headline), transform: `translateY(${(1-headline)*40}px)`}}>근거는 추적하고,<br/>최종 판단은 사람이.</div>
        <div style={{...liftFor(frame, 42, 22), marginTop: 48, display: 'flex', gap: 14}}>
          {['NCS 직접 근거','외부 AI 미사용','source_ref 추적','draft 전용'].map((item, index) => <span key={item} style={{padding: '13px 19px', border: '1px solid rgba(255,255,255,.23)', borderRadius: 999, color: index === 0 ? C.greenDark : '#fff', background: index === 0 ? C.lime : 'rgba(255,255,255,.08)', fontSize: 18, fontWeight: 900}}>{item}</span>)}
        </div>
      </div>
      <div style={{position: 'absolute', left: 160, right: 160, bottom: 75, paddingTop: 24, display: 'flex', justifyContent: 'space-between', borderTop: '1px solid rgba(255,255,255,.19)', color: '#c8dbcf', fontSize: 17}}><span>공고문에서 NCS 근거가 추적되는 직무기술서·직무명세서 초안 생성</span><span>localhost · read-only NCS MCP</span></div>
    </AbsoluteFill>
  );
};

export const NcsJdDemo = () => {
  return (
    <AbsoluteFill style={{...base, background: C.wash}}>
      <Audio src={staticFile('demo-bed.wav')} volume={0.9}/>
      <Sequence from={0} durationInFrames={180}><IntroScene/></Sequence>
      <Sequence from={150} durationInFrames={360}><InputScene/></Sequence>
      <Sequence from={480} durationInFrames={300}><PipelineScene/></Sequence>
      <Sequence from={750} durationInFrames={390}><EvidenceScene/></Sequence>
      <Sequence from={1110} durationInFrames={240}><ResultScene/></Sequence>
      <Sequence from={1320} durationInFrames={180}><OutroScene/></Sequence>
    </AbsoluteFill>
  );
};
