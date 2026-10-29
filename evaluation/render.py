"""Standalone figures distinguish pending quality/cost from observed resource use."""

from pathlib import Path


def render(analysis, directory: Path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory.mkdir(parents=True, exist_ok=True)
    methods = ["fixed", "single_agent", "parallel_tools", "multi_agent"]
    summary = analysis["summary"]["arms"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), constrained_layout=True)
    quality_points = 0
    for method in methods:
        arm = summary[method]
        quality = arm["formal_task_completion_rate"]
        cost = arm["provider_cost_total"]
        if quality is not None and cost is not None:
            axes[0].scatter(cost, quality, label=method)
            quality_points += 1
    axes[0].set(
        xlabel="Verified monetary provider cost",
        ylabel="Independently reviewed task completion",
        ylim=(-0.03, 1.03),
        title="Quality vs cost",
    )
    if quality_points:
        axes[0].legend()
    else:
        axes[0].text(
            0.5,
            0.5,
            "Pending independent reviews\nand verified prices\n\nNo quality or cost values inferred",
            ha="center",
            va="center",
            transform=axes[0].transAxes,
        )
        axes[0].set_xticks([])
    states = ["not_started", "running", "unknown", "failed", "guarded", "completed"]
    bottom = [0] * len(methods)
    for state in states:
        values = [summary[m]["states"].get(state, 0) for m in methods]
        axes[1].bar(methods, values, bottom=bottom, label=state)
        bottom = [a + b for a, b in zip(bottom, values, strict=True)]
    axes[1].set(
        ylabel="Frozen cells, including failures/unrun",
        title="Execution observations; not answer quality",
    )
    axes[1].tick_params(axis="x", labelrotation=20)
    axes[1].legend(fontsize=7)
    fig.suptitle(f"{analysis['mode']} | {analysis['experiment_id'][:12]}", fontsize=11)
    paths = []
    for suffix in ["png", "pdf"]:
        path = directory / ("quality-cost." + suffix)
        fig.savefig(path, dpi=160)
        paths.append(path.as_posix())
    plt.close(fig)
    return paths
