import make_predictions_rust
from smartscore.service import get_min_max


def test_predict():
    assert 1 == 1


def get_rust_weights():
    return make_predictions_rust.Weights(
        gpg=0.3,
        five_gpg=0.4,
        hgpg=0.3,
        tgpg=0.0,
        otga=0.0,
        hppg_otshga=0.0,
        is_home=0.0,
    )


def get_rust_min_max():
    min_max_vals = get_min_max()
    return make_predictions_rust.MinMax(
        min_gpg=min_max_vals["gpg"]["min"],
        max_gpg=min_max_vals["gpg"]["max"],
        min_hgpg=min_max_vals["hgpg"]["min"],
        max_hgpg=min_max_vals["hgpg"]["max"],
        min_five_gpg=min_max_vals["five_gpg"]["min"],
        max_five_gpg=min_max_vals["five_gpg"]["max"],
        min_tgpg=min_max_vals["tgpg"]["min"],
        max_tgpg=min_max_vals["tgpg"]["max"],
        min_otga=min_max_vals["otga"]["min"],
        max_otga=min_max_vals["otga"]["max"],
        min_hppg=min_max_vals["hppg"]["min"],
        max_hppg=min_max_vals["hppg"]["max"],
        min_otshga=min_max_vals["otshga"]["min"],
        max_otshga=min_max_vals["otshga"]["max"],
    )


def get_rust_players():
    rust_players = []

    rust_players.append(
        make_predictions_rust.PlayerInfo(
            gpg=0.375,
            hgpg=0.43209876543209874,
            five_gpg=0.4,
            tgpg=3.2,
            otga=3.18461,
            otshga=0.7230769230769231,
            hppg=0.09053497942386832,
            is_home=True,
            hppg_otshga=0.0,
        )
    )

    rust_players.append(
        make_predictions_rust.PlayerInfo(
            gpg=0.16923076923076924,
            hgpg=0.16666666666666666,
            five_gpg=0.0,
            tgpg=3.2,
            otga=3.18461,
            otshga=0.7230769230769231,
            hppg=0.045454545454545456,
            is_home=True,
            hppg_otshga=0.0,
        )
    )

    rust_players.append(
        make_predictions_rust.PlayerInfo(
            gpg=0.10256410256410256,
            hgpg=0.18238993710691823,
            five_gpg=0.0,
            tgpg=3.2,
            otga=3.18461,
            otshga=0.7230769230769231,
            hppg=0.0,
            is_home=True,
            hppg_otshga=0.0,
        )
    )

    rust_players.append(
        make_predictions_rust.PlayerInfo(
            gpg=0.16393442622950818,
            hgpg=0.23741007194244604,
            five_gpg=0.0,
            tgpg=3.2,
            otga=3.18461,
            otshga=0.7230769230769231,
            hppg=0.03597122302158273,
            is_home=True,
            hppg_otshga=0.0,
        )
    )

    rust_players.append(
        make_predictions_rust.PlayerInfo(
            gpg=0.13953488372093023,
            hgpg=0.16030534351145037,
            five_gpg=0.6,
            tgpg=3.2,
            otga=3.18461,
            otshga=0.7230769230769231,
            hppg=0.0,
            is_home=True,
            hppg_otshga=0.0,
        )
    )

    return rust_players


def test_predict():
    rust_probabilities = make_predictions_rust.predict(get_rust_players(), get_rust_min_max(), get_rust_weights())
    expected_probabilities = [
        0.2010648250579834,
        0.05038461834192276,
        0.04274310916662216,
        0.060201674699783325,
        0.16497603058815002,
    ]
    for i in range(5):
        assert rust_probabilities[i] == expected_probabilities[i]
