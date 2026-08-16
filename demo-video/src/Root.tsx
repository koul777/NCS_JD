import {Composition} from 'remotion';
import {NcsJdDemo} from './NcsJdDemo';

export const RemotionRoot = () => {
  return (
    <Composition
      id="NcsJdDemo"
      component={NcsJdDemo}
      durationInFrames={1500}
      fps={30}
      width={1920}
      height={1080}
    />
  );
};
