
from caproto.server import pvproperty, PVGroup, ioc_arg_parser, run
import random
import logging
from hiden import HidenHPR20Interface

logging.basicConfig(level=logging.INFO)

class RGA:
    @staticmethod
    def readValue(channel):
        return random.uniform(0, 1)

# Create a dictionary to hold PV properties and scan methods
pv_defs = {}

for i in range(1, 11):
    pvname = f'mid{i}_i'
    caproto_pv = pvproperty(
        name=f'MID{i}-I',
        value=0.0,
        doc=f"RGA reading for MID{i}"
    )
    pv_defs[pvname] = caproto_pv

    scan_method = make_scan_method(i)
    pv_defs[f'_mid{i}_i'] = caproto_pv.scan(period=1.0)(scan_method)

    # define a scan method bound to this pv
    def make_scan_method(idx):
        async def scan(self, instance, async_lib):
            new_val = RGA.readValue(channel=idx)
            logging.info(f"Updating MID{idx}-I: {new_val:.4f}")
            await instance.write(new_val)

        return scan

    # assign the scan method with correct decorator
    pv_defs[f'_mid{i}_i'] = caproto_pv.scan(period=1.0)(make_scan)

# Dynamically create the IOC class
RGAIOC = type("RGAIOC", (PVGroup,), pv_defs)

if __name__ == '__main__':
    hiden = HidenHPR20Interface(1)

    ioc_options, run_options = ioc_arg_parser(
        default_prefix='XF:08IDB-SE{{RGA:1}}P:',
        desc="IOC for RGA MID1–10 PVs"
    )
    ioc = RGAIOC(**ioc_options)
    run(ioc.pvdb, **run_options)




#
# from caproto.server import pvproperty, PVGroup, ioc_arg_parser, run
# import random
# import logging
#
# logging.basicConfig(level=logging.INFO)
#
# class RGA:
#     @staticmethod
#     def readValue(channel=1):
#         return random.uniform(0, 1)
#
# class MyIOC(PVGroup):
#     mid1_i = pvproperty(
#         name='MID1-I',
#         value=0.0,
#         doc="RGA reading for MID1"
#     )
#
#     @mid1_i.scan(period=1.0)
#     async def _mid1_i(self, instance, async_lib):
#         new_value = RGA.readValue(channel=1)
#         logging.info(f"Updating MID1-I to {new_value:.4f}")
#         await instance.write(new_value)
#
# if __name__ == '__main__':
#     ioc_options, run_options = ioc_arg_parser(
#         default_prefix='XF:08IDB-SE{{RGA:1}}P:',
#         desc="IOC for RGA MID1 PV"
#     )
#     ioc = MyIOC(**ioc_options)
#     run(ioc.pvdb, **run_options)
#
