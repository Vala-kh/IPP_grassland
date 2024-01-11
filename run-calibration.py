import capnp
from collections import defaultdict
import json
import csv
import matplotlib.pyplot as plt
import monica_run_lib
import os
from pathlib import Path
import spotpy
import subprocess as sp
import sys
import time
import uuid

import calibration_spotpy_setup_MONICA

PATH_TO_REPO = Path(os.path.realpath(__file__)).parent
PATH_TO_MAS_INFRASTRUCTURE_REPO = PATH_TO_REPO / "../mas-infrastructure"
PATH_TO_PYTHON_CODE = PATH_TO_MAS_INFRASTRUCTURE_REPO / "src/python"
if str(PATH_TO_PYTHON_CODE) not in sys.path:
    sys.path.insert(1, str(PATH_TO_PYTHON_CODE))

from pkgs.common import common

PATH_TO_CAPNP_SCHEMAS = (PATH_TO_MAS_INFRASTRUCTURE_REPO / "capnproto_schemas").resolve()
abs_imports = [str(PATH_TO_CAPNP_SCHEMAS)]
fbp_capnp = capnp.load(str(PATH_TO_CAPNP_SCHEMAS / "fbp.capnp"), imports=abs_imports)


def get_reader_writer_srs_from_channel(path_to_channel_binary, chan_name=None):
    chan = sp.Popen([
        path_to_channel_binary,
        "--name=chan_{}".format(chan_name if chan_name else str(uuid.uuid4())),
        "--output_srs",
    ], stdout=sp.PIPE, text=True)
    reader_sr = None
    writer_sr = None
    while True:
        s = chan.stdout.readline().split("=", maxsplit=1)
        id, sr = s if len(s) == 2 else (None, None)
        if id and id == "readerSR":
            reader_sr = sr.strip()
        elif id and id == "writerSR":
            writer_sr = sr.strip()
        if reader_sr and writer_sr:
            break
    return {"chan": chan, "reader_sr": reader_sr, "writer_sr": writer_sr}


local_run = False


def run_calibration(server=None, prod_port=None, cons_port=None):
    config = {
        "mode": "mbm-local-remote",
        "prod-port": prod_port if prod_port else "6666",  # local: 6667, remote 6666
        "cons-port": cons_port if cons_port else "7777",  # local: 6667, remote 6666
        "server": server if server else "login01.cluster.zalf.de",
        "sim.json": "sim.json",
        "crop.json": "crop.json",
        "site.json": "site.json",
        "setups-file": "sim_setups_calibration.csv",
        "path_to_out": "out/",
        "run-setups": "[1]",
        "path_to_channel": "/home/berg/GitHub/mas-infrastructure/src/cpp/common/_cmake_debug/channel" if local_run else
        "/home/rpm/start_manual_test_services/GitHub/mas-infrastructure/src/cpp/common/_cmake_release/channel",
        "path_to_python": "python" if local_run else "/home/rpm/.conda/envs/py39/bin/python",
        "repetitions": "2",
        "test_mode": "false",
        "all_nuts3_regions_one_by_one": False,
        "only_nuts3_region_ids": "[236]",  # "[]",
    }

    common.update_config(config, sys.argv, print_config=True, allow_new_keys=False)

    if not os.path.exists(config["path_to_out"]):
        try:
            os.makedirs(config["path_to_out"])
        except OSError:
            print("run-calibration.py: Couldn't create dir:", config["path_to_out"], "!")
    path_to_out_file = config["path_to_out"] + "/producer.out"
    with open(path_to_out_file, "a") as _:
        _.write(f"config: {config}\n")

    procs = []

    prod_chan_data = get_reader_writer_srs_from_channel(config["path_to_channel"], "prod_chan")
    procs.append(prod_chan_data["chan"])
    cons_chan_data = get_reader_writer_srs_from_channel(config["path_to_channel"], "cons_chan")
    procs.append(cons_chan_data["chan"])

    procs.append(sp.Popen([
        config["path_to_python"],
        "run-producer_calibration.py",
        "mode=mbm-local-remote" if local_run else "mode=hpc-local-remote",
        f"server={config['server']}",
        f"port={config['prod-port']}",
        f"setups-file={config['setups-file']}",
        f"run-setups={config['run-setups']}",
        f"reader_sr={prod_chan_data['reader_sr']}",
        f"test_mode={config['test_mode']}",
        f"path_to_out={config['path_to_out']}",
    ]))

    procs.append(sp.Popen([
        config["path_to_python"],
        "run-consumer_calibration.py",
        # "mode=remoteConsumer-remoteMonica",
        f"server={config['server']}",
        f"port={config['cons-port']}",
        f"run-setups={config['run-setups']}",
        f"writer_sr={cons_chan_data['writer_sr']}",
        f"path_to_out={config['path_to_out']}",
    ]))

    crop_to_observations = defaultdict(list)
    nuts3_region_id_to_name = {}
    with (open("Winter_Wheat_Calibration.csv") as file):
        dialect = csv.Sniffer().sniff(file.read(), delimiters=';,\t')
        file.seek(0)
        reader = csv.reader(file, dialect)
        next(reader, None)  # skip the header
        for row in reader:
            id = int(row[26])
            name = row[1].strip()
            nuts3_region_id_to_name[id] = name
            for i in range(2, 26):
                yield_t = float(row[i])
                crop_to_observations["WW"].append({
                    "id": id,
                    "year": 1999 + i - 2,
                    "value": yield_t if yield_t < 0.0 else yield_t * 1000.0  # t/ha -> kg/ha
                })

    # order obs list by id to avoid mismatch between observation/evaluation lists
    for crop, obs in crop_to_observations.items():
        obs.sort(key=lambda r: [r["id"], r["year"]])

    # read parameters which are to be calibrated
    params = []
    with open("calibratethese.csv") as params_csv:
        dialect = csv.Sniffer().sniff(params_csv.read(), delimiters=';,\t')
        params_csv.seek(0)
        reader = csv.reader(params_csv, dialect)
        next(reader, None)  # skip the header
        for row in reader:
            p = {"name": row[0]}
            if len(row[1]) > 0:
                p["array"] = int(row[1])
            for n, i in [("low", 2), ("high", 3), ("step", 4), ("optguess", 5), ("minbound", 6), ("maxbound", 7)]:
                if len(row[i]) > 0:
                    p[n] = float(row[i])
            if len(row) == 9 and len(row[8]) > 0:
                p["derive_function"] = lambda _, _2: eval(row[8])
            params.append(p)

    con_man = common.ConnectionManager()

    setups = monica_run_lib.read_sim_setups(config["setups-file"])
    run_setups = json.loads(config["run-setups"])
    if len(config["run-setups"]) < 1:
        return
    setup_id = run_setups[0]
    setup = setups[setup_id]
    cons_reader = con_man.try_connect(cons_chan_data["reader_sr"], cast_as=fbp_capnp.Channel.Reader, retry_secs=1)
    prod_writer = con_man.try_connect(prod_chan_data["writer_sr"], cast_as=fbp_capnp.Channel.Writer, retry_secs=1)

    # configure MONICA setup for spotpy
    observations = crop_to_observations[setup["crop-id"][:2]]
    only_nuts3_region_ids = json.loads(config["only_nuts3_region_ids"])

    to_be_run_only_nuts3_region_ids = []
    if config["all_nuts3_regions_one_by_one"]:
        to_be_run_only_nuts3_region_ids = list([id] for id in sorted(nuts3_region_id_to_name.keys()))
    else:
        to_be_run_only_nuts3_region_ids = [only_nuts3_region_ids]

    path_to_out_folder = f"{config['path_to_out']}"
    if not os.path.exists(path_to_out_folder):
        try:
            os.makedirs(path_to_out_folder)
        except OSError:
            print("run-calibration.py: Couldn't create dir:", path_to_out_folder, "!")

    spot_setup = None
    for current_only_nuts3_region_ids in to_be_run_only_nuts3_region_ids:
        nuts3_region_folder_name = "-".join(map(str, current_only_nuts3_region_ids))
        filtered_observations = observations
        if len(only_nuts3_region_ids) > 0:
            filtered_observations = list(filter(lambda d: d["id"] in current_only_nuts3_region_ids, observations))
            if len(filtered_observations) == 0:
                continue
        if spot_setup:
            del spot_setup
        spot_setup = calibration_spotpy_setup_MONICA.spot_setup(params, filtered_observations, prod_writer, cons_reader,
                                                                config["path_to_out"], current_only_nuts3_region_ids)

        rep = int(config["repetitions"]) #initial number was 10
        results = []
        #Set up the sampler with the model above
        sampler = spotpy.algorithms.sceua(spot_setup, dbname=f"{path_to_out_folder}/{nuts3_region_folder_name}_SCEUA_monica_results", dbformat="csv")

        #Run the sampler to produce the paranmeter distribution
        #and identify optimal parameters based on objective function
        #ngs = number of complexes
        #kstop = max number of evolution loops before convergence
        #peps = convergence criterion
        #pcento = percent change allowed in kstop loops before convergence
        sampler.sample(rep, ngs=len(params)*2, peps=0.001, pcento=0.001)

        def print_status_final(self, stream):
            print("\n*** Final SPOTPY summary ***")
            print(
                "Total Duration: "
                + str(round((time.time() - self.starttime), 2))
                + " seconds"
            , file=stream)
            print("Total Repetitions:", self.rep, file=stream)

            if self.optimization_direction == "minimize":
                print("Minimal objective value: %g" % (self.objectivefunction_min), file=stream)
                print("Corresponding parameter setting:", file=stream)
                for i in range(self.parameters):
                    text = "%s: %g" % (self.parnames[i], self.params_min[i])
                    print(text, file=stream)

            if self.optimization_direction == "maximize":
                print("Maximal objective value: %g" % (self.objectivefunction_max), file=stream)
                print("Corresponding parameter setting:", file=stream)
                for i in range(self.parameters):
                    text = "%s: %g" % (self.parnames[i], self.params_max[i])
                    print(text, file=stream)

            if self.optimization_direction == "grid":
                print("Minimal objective value: %g" % (self.objectivefunction_min), file=stream)
                print("Corresponding parameter setting:", file=stream)
                for i in range(self.parameters):
                    text = "%s: %g" % (self.parnames[i], self.params_min[i])
                    print(text, file=stream)

                print("Maximal objective value: %g" % (self.objectivefunction_max), file=stream)
                print("Corresponding parameter setting:", file=stream)
                for i in range(self.parameters):
                    text = "%s: %g" % (self.parnames[i], self.params_max[i])
                    print(text, file=stream)

            print("******************************\n", file=stream)


        path_to_best_out_file = f"{path_to_out_folder}/{nuts3_region_folder_name}_best.out"
        with open(path_to_best_out_file, "a") as _:
            print_status_final(sampler.status, _)

        #Extract the parameter samples from distribution
        results = spotpy.analyser.load_csv_results(f"{path_to_out_folder}/{nuts3_region_folder_name}_SCEUA_monica_results")

        # Plot how the objective function was minimized during sampling
        #font = {"family": "calibri",
        #        "weight": "normal",
        #        "size": 18}
        fig = plt.figure(1, figsize=(9, 6))
        #plt.plot(results["like1"],  marker='o')
        plt.plot(results["like1"], "r+")
        plt.show()
        plt.ylabel("RMSE")
        plt.xlabel("Iteration")
        fig.savefig(f"{path_to_out_folder}/{nuts3_region_folder_name}_SCEUA_objectivefunctiontrace_MONICA.png", dpi=150)

        del results
    # kill the two channels and the producer and consumer
    for proc in procs:
        proc.terminate()

    print("sampler_MONICA.py finished")

if __name__ == "__main__":
    run_calibration()


