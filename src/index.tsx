import { callable, definePlugin } from '@decky/api';
import { PanelSection, PanelSectionRow, SliderField, ToggleField, staticClasses } from '@decky/ui';
import { useEffect, useRef, useState } from 'react';
import { FaSlidersH } from 'react-icons/fa';

type Brightness = { supported: boolean; value: number | null };
type Volume = { supported: boolean; level: number; muted: boolean };
type Radio = { present: boolean; on: boolean; disabled?: boolean };
type Radios = { wifi: Radio; bluetooth: Radio };
type Battery = { supported: boolean; reason: string; error: string | null; limit: number | null };
type SetResult = { ok: boolean; detail?: string };

const getBrightness = callable<[], Brightness>('get_brightness');
const setBrightness = callable<[value: number], boolean>('set_brightness');
const getVolume = callable<[], Volume>('get_volume');
const setVolume = callable<[level: number], boolean>('set_volume');
const setMute = callable<[muted: boolean], boolean>('set_mute');
const getRadios = callable<[], Radios>('get_radios');
const setRadio = callable<[kind: 'wifi' | 'bluetooth', on: boolean], SetResult>('set_radio');
const getBattery = callable<[], Battery>('get_battery');
const setBatteryLimit = callable<[limit: number], SetResult>('set_battery_limit');

// Sliders fire on every step; send at most one value per `ms`, always ending with the last one.
function useThrottled(fn: (v: number) => unknown, ms: number) {
  const pending = useRef<number | null>(null);
  const timer = useRef<number | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  // On unmount (panel closed mid-drag), send the last value instead of dropping it.
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    if (pending.current !== null) fnRef.current(pending.current);
    pending.current = null;
  }, []);
  return (v: number) => {
    pending.current = v;
    if (timer.current !== null) return;
    timer.current = window.setTimeout(() => {
      timer.current = null;
      if (pending.current !== null) fn(pending.current);
      pending.current = null;
    }, ms);
  };
}

function BrightnessSection() {
  const [state, setState] = useState<Brightness | null>(null);
  const send = useThrottled((v) => setBrightness(v), 300);
  useEffect(() => {
    getBrightness().then(setState);
  }, []);
  if (!state?.supported) return null;
  return (
    <PanelSection title="Display">
      <PanelSectionRow>
        <SliderField
          label="Brightness"
          value={state.value ?? 0}
          min={0}
          max={100}
          step={5}
          showValue
          valueSuffix="%"
          onChange={(v) => {
            setState({ supported: true, value: v });
            send(v);
          }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function VolumeSection() {
  const [state, setState] = useState<Volume | null>(null);
  const send = useThrottled((v) => setVolume(v), 100);
  useEffect(() => {
    getVolume().then(setState);
  }, []);
  if (!state?.supported) return null;
  return (
    <PanelSection title="Sound">
      <PanelSectionRow>
        <SliderField
          label="Volume"
          value={state.level}
          min={0}
          max={100}
          step={2}
          showValue
          valueSuffix="%"
          onChange={(v) => {
            setState({ ...state, level: v });
            send(v);
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="Mute"
          checked={state.muted}
          onChange={async (muted) => {
            setState({ ...state, muted });
            try {
              await setMute(muted);
            } finally {
              getVolume()
                .then(setState)
                .catch(() => undefined);
            }
          }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function RadioToggle({ kind, label, radio, refresh }: { kind: 'wifi' | 'bluetooth'; label: string; radio: Radio; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!radio.present) return null;
  return (
    <PanelSectionRow>
      <ToggleField
        label={label}
        description={radio.disabled ? 'Disabled in Device Manager' : (error ?? undefined)}
        checked={radio.on}
        disabled={busy || radio.disabled}
        onChange={async (on) => {
          setBusy(true);
          setError(null);
          try {
            const res = await setRadio(kind, on);
            if (!res.ok) setError(`Windows refused: ${res.detail ?? 'unknown'}`);
          } catch {
            setError('The request failed.');
          } finally {
            setBusy(false);
            refresh();
          }
        }}
      />
    </PanelSectionRow>
  );
}

function RadiosSection() {
  const [state, setState] = useState<Radios | null>(null);
  const refresh = () => {
    getRadios()
      .then(setState)
      .catch(() => undefined);
  };
  useEffect(refresh, []);
  if (!state || (!state.wifi.present && !state.bluetooth.present)) return null;
  return (
    <PanelSection title="Wireless">
      <RadioToggle kind="wifi" label="Wi-Fi" radio={state.wifi} refresh={refresh} />
      <RadioToggle kind="bluetooth" label="Bluetooth" radio={state.bluetooth} refresh={refresh} />
    </PanelSection>
  );
}

function BatterySection() {
  const [state, setState] = useState<Battery | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    getBattery().then(setState);
  }, []);
  // Shown only when the read-only probe found the ASUS charge-limit device.
  if (!state?.supported) return null;
  return (
    <PanelSection title="Battery">
      <PanelSectionRow>
        <ToggleField
          label="Limit charging to 80%"
          description={
            error ??
            (state.limit === null
              ? 'Not set from here yet. The firmware may reset it after a restart.'
              : `Last set to ${state.limit}%. The firmware may reset it after a restart.`)
          }
          checked={state.limit === 80}
          disabled={busy}
          onChange={async (on) => {
            setBusy(true);
            setError(null);
            try {
              const res = await setBatteryLimit(on ? 80 : 100);
              if (!res.ok) setError('The firmware did not accept the new limit.');
              setState(await getBattery());
            } catch {
              setError('The request failed.');
            } finally {
              setBusy(false);
            }
          }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  return (
    <>
      <BrightnessSection />
      <VolumeSection />
      <RadiosSection />
      <BatterySection />
    </>
  );
}

export default definePlugin(() => ({
  name: 'Windows Quick Settings',
  titleView: <div className={staticClasses.Title}>Windows Quick Settings</div>,
  content: <Content />,
  icon: <FaSlidersH />,
}));
