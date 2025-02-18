import json
import torch

from dataclasses import dataclass

from typing import Any, Sequence

from meloetta.frameworks.nash_ketchum.modelv2 import config
from meloetta.frameworks.nash_ketchum import utils


def is_jsonable(x):
    try:
        json.dumps(x)
        return True
    except:
        return False


@dataclass
class AdamConfig:
    """Adam optimizer related params."""

    b1: float = 0.0
    b2: float = 0.999
    eps: float = 10e-8
    weight_decay: float = 1e-5


@dataclass
class NerdConfig:
    """Nerd related params."""

    beta: float = 2.0
    clip: float = 10_000


@dataclass
class NAshKetchumConfig:
    """Learning pararms"""

    # The batch size to use when learning/improving parameters.
    batch_size: int = 8
    # The learning rate for `params`.
    learning_rate: float = 5e-5
    # The config related to the ADAM optimizer used for updating `params`.
    adam: AdamConfig = AdamConfig()
    # All gradients values are clipped to [-clip_gradient, clip_gradient].
    clip_gradient: float = 10_000
    # The "speed" at which `params_target` is following `params`.
    target_network_avg: float = 1e-3

    # RNaD algorithm configuration.
    # Entropy schedule configuration. See EntropySchedule class documentation.
    entropy_schedule_repeats: Sequence[int] = (
        # 10,
        # 9,
        1,
    )
    entropy_schedule_size: Sequence[int] = (
        # 100,
        1000,
        # 10000,
        # 25000,
    )
    # The weight of the reward regularisation term in RNaD.
    eta_reward_transform: float = 0.2
    gamma: float = 1.0
    nerd: NerdConfig = NerdConfig()

    lambda_vtrace: float = 1.0
    c_vtrace: float = 1.0
    rho_vtrace: float = torch.inf

    trajectory_length: int = 1024

    battle_format: str = "gen9randombattle"
    # battle_format: str = "gen3randombattle"
    # battle_format: str = "gen8randomdoublesbattle"
    # battle_format: str = "gen8randombattle"
    # battle_format: str = "gen8ou"
    # battle_format: str = "gen9ou"
    # battle_format: str = "gen8doublesou"
    # battle_format: str = "gen9doublesou"

    gen, gametype = utils.get_gen_and_gametype(battle_format)

    team: str = "null"
    # team = "charizard||heavydutyboots|blaze|furyswipes,scaleshot,toxic,roost||85,,85,85,85,85||,0,,,,||88|"
    # team = "charizard||heavydutyboots|blaze|hurricane,fireblast,toxic,roost||85,,85,85,85,85||,0,,,,||88|]venusaur||blacksludge|chlorophyll|leechseed,substitute,sleeppowder,sludgebomb||85,,85,85,85,85||,0,,,,||82|]blastoise||whiteherb|torrent|shellsmash,earthquake,icebeam,hydropump||85,85,85,85,85,85||||86|"
    # team = "charizard||heavydutyboots|blaze|hurricane,fireblast,toxic,roost||85,,85,85,85,85||,0,,,,||88|]blastoise||whiteherb|torrent|shellsmash,earthquake,icebeam,hydropump||85,85,85,85,85,85||||86|"
    # team = "ceruledge||lifeorb|weakarmor|bitterblade,closecombat,shadowsneak,swordsdance||85,85,85,85,85,85||||82|,,,,,fighting]grafaiai||leftovers|prankster|encore,gunkshot,knockoff,partingshot||85,85,85,85,85,85||||86|,,,,,dark]greedent||sitrusberry|cheekpouch|bodyslam,psychicfangs,swordsdance,firefang||85,85,85,85,85,85||||88|,,,,,psychic]quaquaval||lifeorb|moxie|aquastep,closecombat,swordsdance,icespinner||85,85,85,85,85,85||||80|,,,,,fighting]flapple||lifeorb|hustle|gravapple,outrage,dragondance,suckerpunch||85,85,85,85,85,85||||84|,,,,,grass]pachirisu||assaultvest|voltabsorb|nuzzle,superfang,thunderbolt,uturn||85,85,85,85,85,85||||94|,,,,,flying"

    actor_device: str = "cpu"
    learner_device: str = "cuda"

    debug_mode = False
    eval_mode = not debug_mode

    # This config will spawn 20 workers with 2 players each
    # for a total of 40 players, playing 20 games.
    # it is recommended to have an even number of players per worker
    num_actors: int = 1 if debug_mode else 12
    num_buffers: int = 64

    model_config: config.NAshKetchumModelConfig = config.NAshKetchumModelConfig()

    eval: bool = (not debug_mode) and eval_mode

    def __getindex__(self, key: str):
        return self.__dict__[key]

    def __repr__(self):
        d = {
            key: value if is_jsonable(value) else repr(value)
            for key, value in self.__dict__.items()
        }
        body = json.dumps(d, indent=4, sort_keys=True)
        return f"NAshKetchumConfig({body})"

    def get(self, key: Any, default=None):
        return self.__dict__.get(key, default)
