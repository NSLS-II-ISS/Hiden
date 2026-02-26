import asyncio
import logging
import os

from caproto.server import PVGroup, pvproperty, ioc_arg_parser, run
from massoft_client import MASsoftClient

logging.basicConfig(level=logging.INFO)

class RGAIOC(PVGroup):
    # ——————————————————————————————
    # Control / Configuration PVs
    # ——————————————————————————————
    initialize = pvproperty(
        name='XF:08IDB-SE{{RGA:1}}:OpenExp',
        value=0,
        doc='Trigger opening of the experiment file',
        dtype=int,
    )

    experiment = pvproperty(
        name='XF:08IDB-SE{{RGA:1}}:ExpName',
        value='file56.exp',
        dtype=str,
        max_length=64,
        doc='Name of the .exp file in MASsoft folder'
    )

    acquire = pvproperty(
        name='XF:08IDB-SE{{RGA:1}}:Acquire',
        value=0,
        doc='Start/stop the acquisition loop',
        dtype=int,
    )

    # ——————————————————————————————
    # MID-I readback PVs (1–10)
    # ——————————————————————————————
    for idx in range(1, 11):
        locals()[f'mid{idx}'] = pvproperty(
            name=f'XF:08IDB-SE{{{{RGA:1}}}}P:MID{idx}-I',
            value=0.0,
            doc=f'RGA reading for MID{idx}',
            dtype=float,
        )
    del idx

    # ——————————————————————————————
    # Mass PVs (1–10)
    # ——————————————————————————————
    for idx in range(1, 11):
        locals()[f'mass{idx}'] = pvproperty(
            name=f'XF:08IDB-VA{{{{RGA:1}}}}Mass:MID{idx}',
            value=0.0,
            doc=f'RGA mass for MID{idx}',
            dtype=float,
        )
    del idx

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Instantiate and connect MASsoft client
        self.client = MASsoftClient()
        self.client.initialize()
        self._running = False
        self._acq_task = None

    @initialize.putter
    async def initialize(self, instance, value):
        """Open the experiment file when 'initialize' PV is set to 1."""
        if int(value):
            fname = self.experiment.value
            logging.info(f"Opening experiment: {fname}")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.client.open_experiment, fname)
        return value

    @acquire.putter
    async def acquire(self, instance, value):
        """Start/stop acquisition loop on 'acquire' PV change."""
        want = bool(int(value))
        loop = asyncio.get_running_loop()

        if want and not self._running:
            logging.info("Starting acquisition loop")
            self._running = True
            self._acq_task = asyncio.create_task(self._acquire_loop())
        elif not want and self._running:
            logging.info("Stopping acquisition loop")
            self._running = False
            if self._acq_task:
                self._acq_task.cancel()
        return value

    async def _acquire_loop(self):
        """1 Hz loop: pull headers, then data, and update PVs."""
        try:
            loop = asyncio.get_running_loop()

            # 1) Get and parse legends → mass PVs
            headers = await loop.run_in_executor(None, self.client.get_legends)
            mass_vals = [
                float(h.split()[-1]) for h in headers if 'mass' in h.lower()
            ][:10]
            logging.info(f"Parsed masses: {mass_vals}")
            for idx, m in enumerate(mass_vals, start=1):
                await getattr(self, f'mass{idx}').write(m)

            # 2) Main data loop
            while self._running:
                # pull one cycle of data
                data = await loop.run_in_executor(None, self.client.get_data, 1, 1, False, False)
                if data:
                    row = data[-1]  # use last cycle
                    if len(row) >= len(mass_vals):
                        for idx, val in enumerate(row, start=1):
                            await getattr(self, f'mid{idx}').write(val)
                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logging.info("Acquisition loop cancelled")
        except Exception as ex:
            logging.error(f"Error in acquisition loop: {ex}")

if __name__ == '__main__':
    ioc_opts, run_opts = ioc_arg_parser(
        default_prefix='',  # full names already include prefix
        desc='RGA MASsoft IOC'
    )
    ioc = RGAIOC(**ioc_opts)
    run(ioc.pvdb, **run_opts)
