import re
import torch

from typing import Dict, List, Any, Sequence, Callable, NamedTuple, Tuple


from meloetta.data import CHOICE_FLAGS


class LoopVTraceCarry(NamedTuple):
    """The carry of the v-trace scan loop."""

    reward: torch.Tensor
    # The cumulated reward until the end of the episode. Uncorrected (v-trace).
    # Gamma discounted and includes eta_reg_entropy.
    reward_uncorrected: torch.Tensor
    next_value: torch.Tensor
    next_v_target: torch.Tensor
    importance_sampling: torch.Tensor


def _player_others(
    player_ids: torch.Tensor, valid: torch.Tensor, player: int
) -> torch.Tensor:
    """A vector of 1 for the current player and -1 for others.

    Args:
      player_ids: Tensor [...] containing player ids (0 <= player_id < N).
      valid: Tensor [...] containing whether these states are valid.
      player: The player id as int.

    Returns:
      player_other: is 1 for the current player and -1 for others [..., 1].
    """
    current_player_tensor = player_ids == player

    res = 2 * current_player_tensor - 1
    res = res * valid
    return torch.unsqueeze(res, dim=-1)


def _where(
    pred: torch.Tensor, true_data: torch.Tensor, false_data: torch.Tensor
) -> torch.Tensor:
    """Similar to jax.where but treats `pred` as a broadcastable prefix."""

    def _where_one(t, f):
        if (
            isinstance(t, LoopVTraceCarry)
            or isinstance(t, tuple)
            or isinstance(t, list)
        ):
            res = [_where_one(ts, fs) for ts, fs in zip(t, f)]
            if isinstance(t, LoopVTraceCarry):
                return LoopVTraceCarry(*res)
            else:
                return res
        else:
            # Expand the dimensions of pred if true_data and false_data are higher rank.
            p = pred.view(pred.shape + (1,) * (len(t.shape) - len(pred.shape)))
            p = p.to(torch.bool)
            return torch.where(p, t, f)

    output = [_where_one(td, fd) for td, fd in zip(true_data, false_data)]
    return output


def scan(f: Callable, init, xs, length=None, reverse: bool = False):
    if xs is None:
        xs = [None] * length
    carry = init
    ys = []

    if reverse:
        xs = list(reversed(list(zip(*xs))))

    for x in xs:
        carry, y = f(carry, x)
        ys.append(y)

    if reverse:
        ys = list(reversed(ys))

    if isinstance(ys[0], list):
        res = [torch.stack([n[i] for n in ys]) for i in range(len(ys[0]))]
    else:
        res = torch.stack(ys)

    return carry, res


def _has_played(
    valid: torch.Tensor, player_id: torch.Tensor, player: int
) -> torch.Tensor:
    """Compute a mask of states which have a next state in the sequence."""
    assert valid.shape == player_id.shape

    def _loop_has_played(carry, x):
        valid, player_id = x
        assert valid.shape == player_id.shape

        our_res = torch.ones_like(player_id)
        opp_res = carry
        reset_res = torch.zeros_like(carry)

        our_carry = carry
        opp_carry = carry
        reset_carry = torch.zeros_like(player_id)

        # pyformat: disable
        return _where(
            valid,
            _where(
                (player_id == player),
                (our_carry, our_res),
                (opp_carry, opp_res),
            ),
            (reset_carry, reset_res),
        )
        # pyformat: enable

    _, result = scan(
        f=_loop_has_played,
        init=torch.zeros_like(player_id[-1]),
        xs=(valid, player_id),
        reverse=True,
    )
    return result


def _policy_ratio(
    pi: torch.Tensor, mu: torch.Tensor, actions_oh: torch.Tensor, valid: torch.Tensor
) -> torch.Tensor:
    """Returns a ratio of policy pi/mu when selecting action a.

    By convention, this ratio is 1 on non valid states
    Args:
      pi: the policy of shape [..., A].
      mu: the sampling policy of shape [..., A].
      actions_oh: a one-hot encoding of the current actions of shape [..., A].
      valid: 0 if the state is not valid and else 1 of shape [...].

    Returns:
      pi/mu on valid states and 1 otherwise. The shape is the same
      as pi, mu or actions_oh but without the last dimension A.
    """
    assert pi.shape == mu.shape == actions_oh.shape
    # assert ((valid,), actions_oh.shape[:-1])

    def _select_action_prob(pi: torch.Tensor) -> torch.Tensor:
        return torch.sum(actions_oh * pi, dim=-1) * valid + ~valid

    pi_actions_prob = _select_action_prob(pi)
    mu_actions_prob = _select_action_prob(mu)
    return pi_actions_prob / mu_actions_prob


@torch.no_grad()
def v_trace(
    v: torch.Tensor,
    valid: torch.Tensor,
    player_id: torch.Tensor,
    acting_policy: torch.Tensor,
    merged_policy: torch.Tensor,
    merged_log_policy: torch.Tensor,
    player_others: torch.Tensor,
    actions_oh: torch.Tensor,
    reward: torch.Tensor,
    player: int,
    # Scalars below.
    eta: float,
    lambda_: float,
    c: float,
    rho: float,
    gamma: float = 1.0,
) -> Tuple[Any, Any, Any]:
    """Custom VTrace for trajectories with a mix of different player steps."""

    has_played = _has_played(valid, player_id, player)

    policy_ratio = _policy_ratio(merged_policy, acting_policy, actions_oh, valid)
    inv_mu = _policy_ratio(
        torch.ones_like(merged_policy), acting_policy, actions_oh, valid
    )

    eta_reg_entropy = (
        -eta
        * torch.sum(merged_policy * merged_log_policy, dim=-1)
        * torch.squeeze(player_others, dim=-1)
    )
    eta_log_policy = -eta * merged_log_policy * player_others

    init_state_v_trace = LoopVTraceCarry(
        reward=torch.zeros_like(reward[-1]),
        reward_uncorrected=torch.zeros_like(reward[-1]),
        next_value=torch.zeros_like(v[-1]),
        next_v_target=torch.zeros_like(v[-1]),
        importance_sampling=torch.ones_like(policy_ratio[-1]),
    )

    def _loop_v_trace(carry: LoopVTraceCarry, x) -> Tuple[LoopVTraceCarry, Any]:
        (
            cs,
            player_id,
            v,
            reward,
            eta_reg_entropy,
            valid,
            inv_mu,
            actions_oh,
            eta_log_policy,
        ) = x

        reward_uncorrected = reward + gamma * carry.reward_uncorrected + eta_reg_entropy
        discounted_reward = reward + gamma * carry.reward

        # V-target:
        our_v_target = (
            v
            + torch.unsqueeze(
                torch.clamp(cs * carry.importance_sampling, max=rho), dim=-1
            )
            * (
                torch.unsqueeze(reward_uncorrected, dim=-1)
                + gamma * carry.next_value
                - v
            )
            + lambda_
            * torch.unsqueeze(
                torch.clamp(cs * carry.importance_sampling, max=c), dim=-1
            )
            * gamma
            * (carry.next_v_target - carry.next_value)
        )

        opp_v_target = torch.zeros_like(our_v_target)
        reset_v_target = torch.zeros_like(our_v_target)

        # Learning output:
        our_learning_output = (
            v
            + eta_log_policy  # value
            + actions_oh  # regularisation
            * torch.unsqueeze(inv_mu, dim=-1)
            * (
                torch.unsqueeze(discounted_reward, dim=-1)
                + gamma
                * torch.unsqueeze(carry.importance_sampling, dim=-1)
                * carry.next_v_target
                - v
            )
        )

        opp_learning_output = torch.zeros_like(our_learning_output)
        reset_learning_output = torch.zeros_like(our_learning_output)

        # State carry:
        our_carry = LoopVTraceCarry(
            reward=torch.zeros_like(carry.reward),
            next_value=v,
            next_v_target=our_v_target,
            reward_uncorrected=torch.zeros_like(carry.reward_uncorrected),
            importance_sampling=torch.ones_like(carry.importance_sampling),
        )
        opp_carry = LoopVTraceCarry(
            reward=eta_reg_entropy + cs * discounted_reward,
            reward_uncorrected=reward_uncorrected,
            next_value=gamma * carry.next_value,
            next_v_target=gamma * carry.next_v_target,
            importance_sampling=cs * carry.importance_sampling,
        )
        reset_carry = init_state_v_trace

        # Invalid turn: init_state_v_trace and (zero target, learning_output)
        # pyformat: disable
        return _where(
            valid,
            _where(
                (player_id == player),
                (our_carry, (our_v_target, our_learning_output)),
                (opp_carry, (opp_v_target, opp_learning_output)),
            ),
            (reset_carry, (reset_v_target, reset_learning_output)),
        )
        # pyformat: enable

    _, (v_target, learning_output) = scan(
        f=_loop_v_trace,
        init=init_state_v_trace,
        xs=(
            policy_ratio,
            player_id,
            v,
            reward,
            eta_reg_entropy,
            valid,
            inv_mu,
            actions_oh,
            eta_log_policy,
        ),
        reverse=True,
    )

    return v_target, has_played, learning_output


def apply_force_with_threshold(
    decision_outputs: torch.Tensor,
    force: torch.Tensor,
    threshold: float,
    threshold_center: torch.Tensor,
) -> torch.Tensor:
    """Apply the force with below a given threshold."""
    can_decrease = decision_outputs - threshold_center > -threshold
    can_increase = decision_outputs - threshold_center < threshold
    force_negative = torch.clamp(force, max=0.0)
    force_positive = torch.clamp(force, min=0.0)
    clipped_force = can_decrease * force_negative + can_increase * force_positive
    return decision_outputs * clipped_force.detach()


def renormalize(loss: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """The `normalization` is the number of steps over which loss is computed."""
    loss = torch.sum(loss * mask)
    return loss


def get_loss_v(
    v_list: Sequence[torch.Tensor],
    v_target_list: Sequence[torch.Tensor],
    mask_list: Sequence[torch.Tensor],
) -> torch.Tensor:
    """Define the loss function for the critic."""
    loss_v_list = []
    for v_n, v_target, mask in zip(v_list, v_target_list, mask_list):
        assert v_n.shape[0] == v_target.shape[0]

        loss_v = torch.unsqueeze(mask, dim=-1) * (v_n - v_target.detach()) ** 2
        loss_v = torch.sum(loss_v)
        loss_v_list.append(loss_v)

    return sum(loss_v_list)


def get_loss_nerd(
    logit_list: Sequence[torch.Tensor],
    policy_list: Sequence[torch.Tensor],
    q_vr_list: Sequence[torch.Tensor],
    valid: torch.Tensor,
    player_ids: Sequence[torch.Tensor],
    legal_actions: torch.Tensor,
    importance_sampling_correction: Sequence[torch.Tensor],
    clip: float = 100,
    threshold: float = 2,
) -> torch.Tensor:
    """Define the nerd loss."""
    assert isinstance(importance_sampling_correction, list)
    loss_pi_list = []
    for k, (logit_pi, pi, q_vr, is_c) in enumerate(
        zip(logit_list, policy_list, q_vr_list, importance_sampling_correction)
    ):
        assert logit_pi.shape[0] == q_vr.shape[0]
        # loss policy
        adv_pi = q_vr - torch.sum(pi * q_vr, dim=-1, keepdim=True)
        adv_pi = is_c * adv_pi  # importance sampling correction
        adv_pi = torch.clip(adv_pi, min=-clip, max=clip)
        adv_pi = adv_pi.detach()

        logits = logit_pi - torch.mean(logit_pi * legal_actions, dim=-1, keepdim=True)

        threshold_center = torch.zeros_like(logits)

        force_threshold = apply_force_with_threshold(
            logits,
            adv_pi,
            threshold,
            threshold_center,
        )
        nerd_loss = torch.sum(legal_actions * force_threshold, axis=-1)
        nerd_loss = -renormalize(nerd_loss, valid * (player_ids == k))
        loss_pi_list.append(nerd_loss)

    return sum(loss_pi_list)


def get_gen_and_gametype(battle_format: str) -> Tuple[int, str]:
    gen = int(re.search(r"gen([0-9])", battle_format).groups()[0])
    if "triples" in battle_format:
        gametype = "triples"
    if "doubles" in battle_format:
        gametype = "doubles"
    else:
        gametype = "singles"
    return gen, gametype


def to_id(string: str):
    return "".join(c for c in string if c.isalnum()).lower()


def _get_private_reserve_size(gen: int):
    if gen == 9:
        return 28
    elif gen == 8:
        return 25
    else:
        return 24


def get_buffer_specs(
    trajectory_length: int,
    gen: int,
    gametype: str,
    private_reserve_size: int,
):
    if gametype != "singles":
        if gametype == "doubles":
            n_active = 2
        else:
            n_active = 3
    else:
        n_active = 1

    buffer_specs = {
        "private_reserve": {
            "size": (trajectory_length, 6, private_reserve_size),
            "dtype": torch.int64,
        },
        "public_n": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_total_pokemon": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_faint_counter": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_side_conditions": {
            "size": (trajectory_length, 2, 18, 3),
            "dtype": torch.int64,
        },
        "public_wisher": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_active": {
            "size": (trajectory_length, 2, n_active, 158),
            "dtype": torch.int64,
        },
        "public_reserve": {
            "size": (trajectory_length, 2, 6, 37),
            "dtype": torch.int64,
        },
        "public_stealthrock": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_spikes": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_toxicspikes": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "public_stickyweb": {
            "size": (trajectory_length, 2),
            "dtype": torch.int64,
        },
        "weather": {
            "size": (trajectory_length,),
            "dtype": torch.int64,
        },
        "weather_time_left": {
            "size": (trajectory_length,),
            "dtype": torch.int64,
        },
        "weather_min_time_left": {
            "size": (trajectory_length,),
            "dtype": torch.int64,
        },
        "pseudo_weather": {
            "size": (trajectory_length, 12, 2),
            "dtype": torch.int64,
        },
        "turn": {
            "size": (trajectory_length,),
            "dtype": torch.int64,
        },
        "turns_since_last_move": {
            "size": (trajectory_length,),
            "dtype": torch.int64,
        },
        "action_type_mask": {
            "size": (trajectory_length, 3),
            "dtype": torch.bool,
        },
        "move_mask": {
            "size": (trajectory_length, 4),
            "dtype": torch.bool,
        },
        "switch_mask": {
            "size": (trajectory_length, 6),
            "dtype": torch.bool,
        },
        "flag_mask": {
            "size": (trajectory_length, len(CHOICE_FLAGS)),
            "dtype": torch.bool,
        },
        "rewards": {
            "size": (trajectory_length, 2),
            "dtype": torch.float32,
        },
        "player_id": {
            "size": (trajectory_length,),
            "dtype": torch.long,
        },
    }
    if gametype != "singles":
        buffer_specs.update(
            {
                "target_mask": {
                    "size": (trajectory_length, 4),
                    "dtype": torch.bool,
                },
                "prev_choices": {
                    "size": (trajectory_length, 2, 4),
                    "dtype": torch.int64,
                },
                "choices_done": {
                    "size": (trajectory_length, 1),
                    "dtype": torch.int64,
                },
                "targeting": {
                    "size": (trajectory_length, 1),
                    "dtype": torch.int64,
                },
                "target_policy": {
                    "size": (trajectory_length, 2 * n_active),
                    "dtype": torch.float32,
                },
                "target_index": {
                    "size": (trajectory_length,),
                    "dtype": torch.int64,
                },
            }
        )

    # add policy...
    buffer_specs.update(
        {
            "action_type_policy": {
                "size": (trajectory_length, 3),
                "dtype": torch.float32,
            },
            "move_policy": {
                "size": (trajectory_length, 4),
                "dtype": torch.float32,
            },
            "switch_policy": {
                "size": (trajectory_length, 6),
                "dtype": torch.float32,
            },
            "flag_policy": {
                "size": (trajectory_length, len(CHOICE_FLAGS)),
                "dtype": torch.float32,
            },
        }
    )

    # ...and indices
    buffer_specs.update(
        {
            "action_type_index": {
                "size": (trajectory_length,),
                "dtype": torch.int64,
            },
            "move_index": {
                "size": (trajectory_length,),
                "dtype": torch.int64,
            },
            "switch_index": {
                "size": (trajectory_length,),
                "dtype": torch.int64,
            },
            "flag_index": {
                "size": (trajectory_length,),
                "dtype": torch.int64,
            },
        }
    )

    if gen == 8:
        buffer_specs.update(
            {
                "max_move_mask": {
                    "size": (trajectory_length, 4),
                    "dtype": torch.bool,
                },
                "max_move_policy": {
                    "size": (trajectory_length, 4),
                    "dtype": torch.float32,
                },
                "max_move_index": {
                    "size": (trajectory_length,),
                    "dtype": torch.int64,
                },
            }
        )

    buffer_specs.update(
        {
            "valid": {
                "size": (trajectory_length,),
                "dtype": torch.bool,
            }
        }
    )
    return buffer_specs


def create_buffers(num_buffers: int, trajectory_length: int, gen: int, gametype: str):
    """
    num_buffers: int
        the size of the replay buffer
    trajectory_length: int
        the max length of a replay
    battle_format: str
        the type of format
    """

    private_reserve_size = _get_private_reserve_size(gen)

    buffer_specs = get_buffer_specs(
        trajectory_length, gen, gametype, private_reserve_size
    )

    buffers: Dict[str, List[torch.Tensor]] = {key: [] for key in buffer_specs}
    for _ in range(num_buffers):
        for key in buffer_specs:
            if key.endswith("_mask"):
                buffers[key].append(torch.ones(**buffer_specs[key]).share_memory_())
            else:
                buffers[key].append(torch.zeros(**buffer_specs[key]).share_memory_())
    return buffers


def expand_bt(tensor: torch.Tensor, time: int = 1, batch: int = 1) -> torch.Tensor:
    shape = tensor.shape
    return tensor.view(time, batch, *(tensor.shape or shape))
