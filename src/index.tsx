import { callable, definePlugin } from '@decky/api';
import { ButtonItem, Field, PanelSection, PanelSectionRow, SliderField, Spinner, ToggleField, staticClasses } from '@decky/ui';
import { useEffect, useRef, useState } from 'react';
import { FaBluetoothB, FaGamepad, FaHeadphones, FaLaptop, FaMobileAlt, FaSlidersH } from 'react-icons/fa';

type Brightness = { supported: boolean; value: number | null };
type Volume = { supported: boolean; level: number; muted: boolean };
type Radio = { present: boolean; on: boolean; disabled?: boolean };
type Radios = { wifi: Radio; bluetooth: Radio };
type Battery = { supported: boolean; reason: string; error: string | null; limit: number | null };
type SetResult = { ok: boolean; detail?: string };
type BtDevice = { id: string; name: string; connected: boolean; type: 'audio' | 'input' | 'phone' | 'computer' | 'other'; le: boolean };
type BtList = { ok: boolean; devices: BtDevice[] };
type Scan = { active: boolean; done: boolean; devices: BtDevice[] };
type Pairing = {
  active: boolean;
  stage: 'idle' | 'starting' | 'pairing' | 'confirm' | 'show_pin' | 'done';
  device_id: string | null;
  name: string;
  pin: string | null;
  kind: string | null;
  ok: boolean | null;
  message: string | null;
};

const getBrightness = callable<[], Brightness>('get_brightness');
const setBrightness = callable<[value: number], boolean>('set_brightness');
const getVolume = callable<[], Volume>('get_volume');
const setVolume = callable<[level: number], boolean>('set_volume');
const setMute = callable<[muted: boolean], boolean>('set_mute');
const getRadios = callable<[], Radios>('get_radios');
const setRadio = callable<[kind: 'wifi' | 'bluetooth', on: boolean], SetResult>('set_radio');
const getBtDevices = callable<[], BtList>('get_bluetooth_devices');
const startScan = callable<[], Scan>('start_scan');
const getScan = callable<[], Scan>('get_scan');
const stopScan = callable<[], Scan>('stop_scan');
const unpairBluetooth = callable<[deviceId: string], { ok: boolean; status: string }>('unpair_bluetooth');
const startPairing = callable<[deviceId: string, name: string], Pairing>('start_pairing');
const getPairing = callable<[], Pairing>('get_pairing');
const respondPairing = callable<[accept: boolean], Pairing>('respond_pairing');
const cancelPairing = callable<[], Pairing>('cancel_pairing');
const dismissPairing = callable<[], Pairing>('dismiss_pairing');
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

function RadiosSection({ state, refresh }: { state: Radios | null; refresh: () => void }) {
  if (!state || (!state.wifi.present && !state.bluetooth.present)) return null;
  return (
    <PanelSection title="Wireless">
      <RadioToggle kind="wifi" label="Wi-Fi" radio={state.wifi} refresh={refresh} />
      <RadioToggle kind="bluetooth" label="Bluetooth" radio={state.bluetooth} refresh={refresh} />
    </PanelSection>
  );
}

function DeviceIcon({ type }: { type: BtDevice['type'] }) {
  switch (type) {
    case 'audio':
      return <FaHeadphones />;
    case 'input':
      return <FaGamepad />;
    case 'phone':
      return <FaMobileAlt />;
    case 'computer':
      return <FaLaptop />;
    default:
      return <FaBluetoothB />;
  }
}

// Windows has no public API to connect or disconnect a paired classic audio device, and an LE device stays connected
// only while some app holds it, so there are no Connect/Disconnect buttons. Classic audio devices get a hint instead.
function statusText(d: BtDevice) {
  if (d.connected) return 'Connected';
  return d.type === 'audio' && !d.le ? 'Not connected. Reconnects automatically when turned on.' : 'Not connected';
}

const PIN_STYLE = { fontSize: '1.4em', fontWeight: 'bold', letterSpacing: '0.1em' } as const;
const SPINNER_STYLE = { width: '24px', height: '24px' } as const;

function PairingPanel({ state, onClose }: { state: Pairing; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch {
      // The next poll shows the real state.
    } finally {
      setBusy(false);
    }
  };
  const name = state.name || 'the device';
  if (state.stage === 'done') {
    return (
      <>
        <PanelSectionRow>
          <Field label={state.ok ? `Paired with ${name}` : `Could not pair with ${name}`} description={state.message ?? undefined} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onClose}>
            OK
          </ButtonItem>
        </PanelSectionRow>
      </>
    );
  }
  if (state.stage === 'confirm') {
    return (
      <>
        <PanelSectionRow>
          <Field label={`Pair with ${name}?`} description="Check that the device shows the same PIN.">
            <span style={PIN_STYLE}>{state.pin}</span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => act(() => respondPairing(true))}>
            PIN matches, pair
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => act(() => respondPairing(false))}>
            Cancel
          </ButtonItem>
        </PanelSectionRow>
      </>
    );
  }
  return (
    <>
      <PanelSectionRow>
        {state.stage === 'show_pin' ? (
          <Field label={`Type this PIN on ${name}`} description="Then press Enter on the device.">
            <span style={PIN_STYLE}>{state.pin}</span>
          </Field>
        ) : (
          <Field label={`Pairing with ${name}...`} description="Keep the device in pairing mode.">
            <Spinner style={SPINNER_STYLE} />
          </Field>
        )}
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={() => act(() => cancelPairing())}>
          Cancel
        </ButtonItem>
      </PanelSectionRow>
    </>
  );
}

function PairedDevice({ device, onRemoved }: { device: BtDevice; onRemoved: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (confirming) {
    return (
      <>
        <PanelSectionRow>
          <Field label={`Remove ${device.name}?`} description="To use it again, you will need to pair it again." icon={<DeviceIcon type={device.type} />} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const res = await unpairBluetooth(device.id);
                if (!res.ok) setError(`Windows could not remove it (${res.status}).`);
              } catch {
                setError('The request failed.');
              } finally {
                setBusy(false);
                setConfirming(false);
                onRemoved();
              }
            }}
          >
            {busy ? 'Removing...' : 'Remove'}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => setConfirming(false)}>
            Keep
          </ButtonItem>
        </PanelSectionRow>
      </>
    );
  }
  return (
    <PanelSectionRow>
      <ButtonItem label={device.name} description={error ?? statusText(device)} icon={<DeviceIcon type={device.type} />} onClick={() => setConfirming(true)}>
        Remove
      </ButtonItem>
    </PanelSectionRow>
  );
}

const pairingFailed = (d: BtDevice, message: string): Pairing => ({
  active: false,
  stage: 'done',
  device_id: d.id,
  name: d.name,
  pin: null,
  kind: null,
  ok: false,
  message,
});

function BluetoothDevicesSection({ radio }: { radio: Radio }) {
  const [paired, setPaired] = useState<BtList | null>(null);
  const [found, setFound] = useState<BtDevice[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const mounted = useRef(true);
  // True while start_pairing is in flight: polls then may still see the previous (or no) session, so ignore them.
  const starting = useRef(false);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  const refresh = () => {
    getBtDevices()
      .then((v) => {
        if (mounted.current) setPaired(v);
      })
      .catch(() => undefined);
  };
  useEffect(() => {
    if (!radio.on) return;
    refresh();
    // Pick up a pairing that was still running (or finished) while the panel was closed.
    getPairing()
      .then((p) => {
        if (mounted.current && !starting.current && p.stage !== 'idle') setPairing(p);
      })
      .catch(() => undefined);
    // And a search that is still running.
    getScan()
      .then((s) => {
        if (mounted.current && s.active) {
          setFound(s.devices);
          setSearching(true);
        }
      })
      .catch(() => undefined);
  }, [radio.on]);

  // Poll while a search runs: found devices show up as Windows reports them. Stops on unmount or when it ends.
  useEffect(() => {
    if (!searching) return;
    const timer = window.setInterval(() => {
      getScan()
        .then((s) => {
          if (!mounted.current) return;
          setFound(s.devices);
          if (!s.active) setSearching(false);
        })
        .catch(() => undefined);
    }, 500);
    return () => window.clearInterval(timer);
  }, [searching]);

  // Poll while a pairing runs; refresh the paired list when it ends.
  const active = pairing?.active ?? false;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      getPairing()
        .then((p) => {
          if (!mounted.current || starting.current) return;
          setPairing(p);
          if (!p.active) {
            refresh();
            if (p.ok) setFound((f) => (f ? f.filter((d) => d.id !== p.device_id) : f));
          }
        })
        .catch(() => undefined);
    }, 500);
    return () => window.clearInterval(timer);
  }, [active]);

  if (!radio.present) return null;
  if (!radio.on) {
    return (
      <PanelSection title="Bluetooth devices">
        <PanelSectionRow>
          <Field label="Bluetooth is off" />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const search = async () => {
    setFound([]);
    setSearching(true);
    try {
      const s = await startScan();
      if (!mounted.current) return;
      setFound(s.devices);
      if (!s.active) setSearching(false);
    } catch {
      if (mounted.current) setSearching(false);
    }
  };

  // Returns once the backend has stopped the search's watchers.
  const stopSearch = async () => {
    setStopping(true);
    try {
      const s = await stopScan();
      if (mounted.current) setFound(s.devices);
    } catch {
      // start_pairing stops a search too; the search also ends by itself.
    } finally {
      if (mounted.current) {
        setStopping(false);
        setSearching(false);
      }
    }
  };

  const pair = async (d: BtDevice) => {
    starting.current = true;
    setPairing({ active: true, stage: 'starting', device_id: d.id, name: d.name, pin: null, kind: null, ok: null, message: null });
    // Searching and pairing fight for the radio.
    if (searching) await stopSearch();
    try {
      const p = await startPairing(d.id, d.name);
      if (mounted.current) setPairing(p);
    } catch {
      if (mounted.current) setPairing(pairingFailed(d, 'The request failed.'));
    } finally {
      starting.current = false;
    }
  };

  const dismiss = () => {
    setPairing(null);
    // Forget the finished pairing in the backend too, so it does not show again when the panel reopens.
    dismissPairing().catch(() => undefined);
  };

  return (
    <PanelSection title="Bluetooth devices">
      {pairing && pairing.stage !== 'idle' ? (
        <PairingPanel state={pairing} onClose={dismiss} />
      ) : (
        <>
          {paired && !paired.ok && (
            <PanelSectionRow>
              <Field label="Could not read the paired devices" />
            </PanelSectionRow>
          )}
          {paired?.ok && paired.devices.length === 0 && (
            <PanelSectionRow>
              <Field label="No paired devices" />
            </PanelSectionRow>
          )}
          {paired?.devices.map((d) => <PairedDevice key={d.id} device={d} onRemoved={refresh} />)}
          {searching ? (
            <>
              <PanelSectionRow>
                <Field label="Searching..." description="Put the device in pairing mode. Devices show up here as they are found.">
                  <Spinner style={SPINNER_STYLE} />
                </Field>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={stopping} onClick={stopSearch}>
                  {stopping ? 'Stopping...' : 'Stop'}
                </ButtonItem>
              </PanelSectionRow>
            </>
          ) : (
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={search}>
                Search for devices
              </ButtonItem>
            </PanelSectionRow>
          )}
          {!searching && found?.length === 0 && (
            <PanelSectionRow>
              <Field label="No new devices found" description="Put the device in pairing mode and search again." />
            </PanelSectionRow>
          )}
          {found?.map((d) => (
            <PanelSectionRow key={d.id}>
              <ButtonItem label={d.name} description={d.le ? 'Bluetooth LE' : 'Bluetooth'} icon={<DeviceIcon type={d.type} />} onClick={() => pair(d)}>
                Pair
              </ButtonItem>
            </PanelSectionRow>
          ))}
        </>
      )}
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
  // Shared by the Wireless switches and the Bluetooth devices section, so turning Bluetooth on or off updates both.
  const [radios, setRadios] = useState<Radios | null>(null);
  const refreshRadios = () => {
    getRadios()
      .then(setRadios)
      .catch(() => undefined);
  };
  useEffect(refreshRadios, []);
  return (
    <>
      <BrightnessSection />
      <VolumeSection />
      <RadiosSection state={radios} refresh={refreshRadios} />
      {radios && <BluetoothDevicesSection radio={radios.bluetooth} />}
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
