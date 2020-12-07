"""
    print(args.chrom)
Various utilities for manipulating tree sequences and running tsinfer.
"""
import argparse
import time
import collections
import json
import os.path

import tskit
import tsinfer
import tsdate
import daiquiri
import numpy as np
import pandas as pd
import tqdm
import humanize
import cyvcf2


def run_simplify(args):
    ts = tskit.load(args.input)
    ts = ts.simplify()
    ts.dump(args.output)


def run_get_dated_samples(args):
    samples = tsinfer.load(args.samples)
    ts = tskit.load(args.ts)
    assert args.samples.endswith(".samples")
    prefix = args.samples[0 : -len(".samples")]
    copy = samples.copy(prefix + ".dated.samples")
    copy.sites_time[:] = tsdate.get_sites_time(ts)
    copy.finalise()


def run_augment(sample_data, ancestors_ts, subset, num_threads):
    progress_monitor = tsinfer.cli.ProgressMonitor(enabled=True, augment_ancestors=True)
    return tsinfer.augment_ancestors(
        sample_data,
        ancestors_ts,
        subset,
        num_threads=num_threads,
        progress_monitor=progress_monitor,
    )


def run_match_samples(sample_data, ancestors_ts, num_threads):
    progress_monitor = tsinfer.cli.ProgressMonitor(enabled=True, match_samples=True)
    return tsinfer.match_samples(
        sample_data,
        ancestors_ts,
        num_threads=num_threads,
        simplify=False,
        progress_monitor=progress_monitor,
    )


def run_sequential_augment(args):

    base = ".".join(args.input.split(".")[:-1])

    sample_data = tsinfer.load(args.input)
    num_samples = sample_data.num_samples
    ancestors_ts = tskit.load(base + ".ancestors.trees")

    # Compute the total samples required.
    n = 2
    total = 0
    while n < num_samples // 4:
        total += n
        n *= 2

    np.random.seed(args.seed)
    samples = np.random.choice(np.arange(num_samples), size=total, replace=False)
    np.save(base + ".augmented_samples.npy", samples)

    n = 2
    j = 0
    while n < num_samples // 4:
        augmented_file = base + ".augmented_{}.ancestors.trees".format(n)
        final_file = base + ".augmented_{}.nosimplify.trees".format(n)
        subset = samples[j : j + n]
        subset.sort()
        ancestors_ts = run_augment(sample_data, ancestors_ts, subset, args.num_threads)
        ancestors_ts.dump(augmented_file)
        j += n
        n *= 2

    final_ts = run_match_samples(sample_data, ancestors_ts, args.num_threads)
    final_ts.dump(final_file)


def run_benchmark_tskit(args):

    before = time.perf_counter()
    ts = tskit.load(args.input)
    duration = time.perf_counter() - before
    print("Loaded in {:.2f}s".format(duration))

    print("num_nodes = ", ts.num_nodes)
    print("num_edges = ", ts.num_edges)
    print("num_trees = ", ts.num_trees)
    print("size = ", humanize.naturalsize(os.path.getsize(args.input), binary=True))

    before = time.perf_counter()
    j = 0
    for tree in ts.trees(sample_counts=False):
        j += 1
    assert j == ts.num_trees
    duration = time.perf_counter() - before
    print("Iterated over trees in {:.2f}s".format(duration))

    before = time.perf_counter()
    num_variants = 0
    # As of msprime 0.6.1, it's a little bit more efficient to specify the full
    # samples and use the tree traversal based decoding algorithm than the full
    # sample-lists for UKBB trees. This'll be fixed in the future.
    for var in ts.variants(samples=ts.samples()):
        if num_variants == args.num_variants:
            break
        num_variants += 1
    duration = time.perf_counter() - before
    total_genotypes = (ts.num_samples * num_variants) / 10 ** 6
    print(
        "Iterated over {} variants in {:.2f}s @ {:.2f} M genotypes/s".format(
            num_variants, duration, total_genotypes / duration
        )
    )


def run_benchmark_vcf(args):

    before = time.perf_counter()
    records = cyvcf2.VCF(args.input)
    duration = time.perf_counter() - before
    print("Read BCF header in {:.2f} seconds".format(duration))
    before = time.perf_counter()
    count = 0
    for record in records:
        count += 1
    duration = time.perf_counter() - before
    print("Read {} VCF records in {:.2f} seconds".format(count, duration))


def get_augmented_samples(tables):
    # Shortcut. Iterating over all the IDs is very slow here.
    # Note that we don't necessarily recover all of the samples that were
    # augmented here because they might have been simplified out.
    # return np.load("ukbb_chr20.augmented_samples.npy")
    nodes = tables.nodes
    ids = np.where(nodes.flags == tsinfer.NODE_IS_SAMPLE_ANCESTOR)[0]
    sample_ids = np.zeros(len(ids), dtype=int)
    for j, node_id in enumerate(tqdm.tqdm(ids)):
        offset = nodes.metadata_offset[node_id : node_id + 2]
        buff = bytearray(nodes.metadata[offset[0] : offset[1]])
        md = json.loads(buff.decode())
        sample_ids[j] = md["sample"]
    return sample_ids


def run_compute_ukbb_gnn(args):
    ts = tskit.load(args.input)
    tables = ts.tables
    before = time.time()
    augmented_samples = set(get_augmented_samples(tables))
    duration = time.time() - before
    print("Got augmented:", len(augmented_samples), "in ", duration)

    reference_sets_map = collections.defaultdict(list)

    ind_metadata = [None for _ in range(ts.num_individuals)]
    all_samples = []
    for ind in ts.individuals():
        md = json.loads(ind.metadata.decode())
        ind_metadata[ind.id] = md
        for node in ind.nodes:
            if node not in augmented_samples:
                reference_sets_map[md["CentreName"]].append(node)
                all_samples.append(node)
    reference_set_names = list(reference_sets_map.keys())
    reference_sets = [reference_sets_map[key] for key in reference_set_names]

    cols = {
        "centre": [
            ind_metadata[ts.node(u).individual]["CentreName"] for u in all_samples
        ],
        "sample_id": [
            ind_metadata[ts.node(u).individual]["SampleID"] for u in all_samples
        ],
        "ethnicity": [
            ind_metadata[ts.node(u).individual]["Ethnicity"] for u in all_samples
        ],
    }
    print("Computing GNNs for ", len(all_samples), "samples")
    before = time.time()
    A = ts.genealogical_nearest_neighbours(
        all_samples, reference_sets, num_threads=args.num_threads
    )
    duration = time.time() - before
    print("Done in {:.2f} mins".format(duration / 60))

    for j, name in enumerate(reference_set_names):
        cols[name] = A[:, j]
    df = pd.DataFrame(cols)
    df.to_csv(args.output)


def run_compute_1kg_gnn(args):
    ts = tskit.load(args.input)

    population_name = []
    region_name = []

    for population in ts.populations():
        md = json.loads(population.metadata.decode())
        name = md["name"]
        population_name.append(name)
        if "super_population" in md:
            region_name.append(md["super_population"])
        elif "region" in md:
            region_name.append(md["region"])
        elif "name" in md:
            region_name.append(md["name"])

    population = []
    region = []
    individual = []
    for j, u in enumerate(ts.samples()):
        node = ts.node(u)
        ind = json.loads(ts.individual(node.individual).metadata.decode())
        if "individual_id" in ind:
            individual.append(ind["individual_id"])
        elif "name" in ind:
            individual.append(ind["name"])
        population.append(population_name[node.population])
        region.append(region_name[node.population])

    sample_sets = [ts.samples(pop) for pop in range(ts.num_populations)]
    print("Computing GNNs")
    before = time.time()
    A = ts.genealogical_nearest_neighbours(
        ts.samples(), sample_sets, num_threads=args.num_threads
    )
    duration = time.time() - before
    print("Done in {:.2f} mins".format(duration / 60))

    cols = {population_name[j]: A[:, j] for j in range(ts.num_populations)}
    cols["population"] = population
    cols["region"] = region
    cols["individual"] = individual
    df = pd.DataFrame(cols)
    df.to_csv(args.output)


def run_compute_sgdp_gnn(args):
    ts = tskit.load(args.input)

    population_name = []
    region_name = []

    for population in ts.populations():
        md = json.loads(population.metadata.decode())
        name = md["name"]
        population_name.append(name)
        region_name.append(md["region"])

    population = []
    region = []
    individual = []
    for j, u in enumerate(ts.samples()):
        node = ts.node(u)
        ind = json.loads(ts.individual(node.individual).metadata.decode())
        individual.append(ind["sgdp_id"])
        population.append(population_name[node.population])
        region.append(region_name[node.population])

    sample_sets = [ts.samples(pop) for pop in range(ts.num_populations)]
    print("Computing GNNs")
    before = time.time()
    A = ts.genealogical_nearest_neighbours(
        ts.samples(), sample_sets, num_threads=args.num_threads
    )
    duration = time.time() - before
    print("Done in {:.2f} mins".format(duration / 60))

    cols = {population_name[j]: A[:, j] for j in range(ts.num_populations)}
    cols["population"] = population
    cols["region"] = region
    cols["individual"] = individual
    df = pd.DataFrame(cols)
    df.to_csv(args.output)


def run_compute_hgdp_gnn(args):
    ts = tskit.load(args.input)

    population_name = []
    region_name = []

    for population in ts.populations():
        md = json.loads(population.metadata.decode())
        name = md["name"]
        population_name.append(name)
        region_name.append(md["region"])

    population = []
    region = []
    individual = []
    for j, u in enumerate(ts.samples()):
        node = ts.node(u)
        ind = json.loads(ts.individual(node.individual).metadata.decode())
        individual.append(ind["sample"])
        population.append(population_name[node.population])
        region.append(region_name[node.population])

    sample_sets = [ts.samples(pop) for pop in range(ts.num_populations)]
    print("Computing GNNs")
    before = time.time()
    A = ts.genealogical_nearest_neighbours(
        ts.samples(), sample_sets, num_threads=args.num_threads
    )
    duration = time.time() - before
    print("Done in {:.2f} mins".format(duration / 60))

    cols = {population_name[j]: A[:, j] for j in range(ts.num_populations)}
    cols["population"] = population
    cols["region"] = region
    cols["individual"] = individual
    df = pd.DataFrame(cols)
    df.to_csv(args.output)


def run_compute_hgdp_1kg_sgdp_gnn(args):
    ts = tskit.load(args.input)

    population_name = []
    region_name = []

    for population in ts.populations():
        md = json.loads(population.metadata.decode())
        name = md["name"]
        population_name.append(name)

        if "super_population" in md:
            region_name.append(md["super_population"])
        elif "region" in md:
            region_name.append(md["region"])

    population = []
    region = []
    for j, u in enumerate(ts.samples()):
        node = ts.node(u)
        population.append(population_name[node.population])
        region.append(region_name[node.population])

    sample_sets = [ts.samples(pop) for pop in range(ts.num_populations)]
    print("Computing GNNs")
    before = time.time()
    A = ts.genealogical_nearest_neighbours(
        ts.samples(), sample_sets, num_threads=args.num_threads
    )
    duration = time.time() - before
    print("Done in {:.2f} mins".format(duration / 60))

    cols = {population_name[j]: A[:, j] for j in range(ts.num_populations)}
    cols["population"] = population
    cols["region"] = region
    df = pd.DataFrame(cols)
    df.to_csv(args.output)


def make_sampledata_compatible(args):
    """
    Make a list of sampledata files compatible with the first file.
    """

    # Load all the sampledata files into a list
    print(
        "Subset sites with {} sampledata files".format(len(args.input_sampledata) - 1)
    )
    for index, fn in enumerate(args.input_sampledata):
        fn = fn.rstrip("\n")
        if index == 0:
            target_sd = tsinfer.load(fn)
            print("Loaded First sampledata file")
            continue
        cur_sd = tsinfer.load(fn)
        print("Loaded sampledata file # {}".format(index))
        keep_sites = np.where(
            np.isin(cur_sd.sites_position[:], target_sd.sites_position[:])
        )[0]
        print("Subsetting to {} sites".format(len(keep_sites)))
        small_cur_sd = cur_sd.subset(sites=keep_sites)
        print("Done with subset")
        newname = fn[: -len(".samples")] + ".subset.samples"
        small_cur_sd_copy = small_cur_sd.copy(newname)
        small_cur_sd_copy.finalise()
        print(
            "Subsetted to {} sites from {}. Output can be found at {}.".format(
                len(keep_sites), fn, newname
            )
        )


def add_indiv_times(args):
    """
    Takes samples 'age' in metadata and add to individuals_time[:]
    """
    samples = tsinfer.load(args.input)
    times = samples.individuals_time[:]
    copy = samples.copy(args.output)
    for indiv in samples.individuals():
        if "age" in indiv.metadata:
            times[indiv.id] = int(indiv.metadata["age"])

    copy.individuals_time[:] = times
    copy.finalise()


def merge_sampledata_files(args):
    samples = []
    for cur_sample in args.input_sampledata:
        samples.append(tsinfer.load(cur_sample))
    merged_samples = samples[0]
    for index, other_samples in enumerate(samples[1:]):
        print("Loaded sampledata file # {}".format(index))
        intersect_sites = np.isin(
            merged_samples.sites_position[:], other_samples.sites_position[:]
        )
        other_intersect_sites = np.where(
            np.isin(other_samples.sites_position[:], merged_samples.sites_position[:])
        )[0]
        other_samples_metadata = other_samples.sites_metadata[:]
        for site_index, site_metadata in zip(
            other_intersect_sites, merged_samples.sites_metadata[:][intersect_sites]
        ):
            other_samples_metadata[site_index] = site_metadata
        other_samples_copy = other_samples.copy()
        other_samples_copy.sites_metadata[:] = other_samples_metadata
        other_samples_copy.finalise()
        merged_samples = merged_samples.merge(other_samples_copy)
        print("Merged sampledata file # {}".format(index))
    merged_copy = merged_samples.copy(args.output)
    merged_copy.finalise()


def remove_moderns_reich(args):
    samples = tsinfer.load(args.input)
    ancients = samples.subset(individuals=np.where(samples.individuals_time[:] != 0)[0])
    genos = ancients.sites_genotypes[:]
    sites = np.where(np.sum(genos == 1, axis=1) != 0)[0]
    ancients_pruned = ancients.subset(sites=sites)
    copy = ancients_pruned.copy(args.output)
    copy.finalise()


def keep_with_offset(keep, data, offset):
    """
    Used when filtering _offset columns in tables
    """
    # We need the astype here for 32 bit machines
    lens = np.diff(offset).astype(np.int32)
    return (
        data[np.repeat(keep, lens)],
        np.concatenate(
            [
                np.array([0], dtype=offset.dtype),
                np.cumsum(lens[keep], dtype=offset.dtype),
            ]
        ),
    )


def get_provenance_dict(parameters=None):
    """
    Returns a dictionary encoding an execution of tskit conforming to the
    provenance schema.
    """
    document = {
        "schema_version": "1.0.0",
        "software": {"name": "tskit", "version": tskit.__version__},
        "parameters": parameters,
    }
    return document


def delete_site_mutations(tables, site_ids, record_provenance=True):
    """
    Remove the mutations at the specified sites entirely from the mutations table in
    this collection.
    :param list[int] site_ids: A list of site IDs specifying the sites whose
        mutations will be removed.
    :param bool record_provenance: If ``True``, add details of this operation
        to the provenance table in this TableCollection. (Default: ``True``).
    """
    keep_sites = np.ones(len(tables.sites), dtype=bool)
    site_ids = site_ids.astype(np.int32)
    if np.any(site_ids < 0) or np.any(site_ids >= len(tables.sites)):
        raise ValueError("Site ID out of bounds")
    keep_sites[site_ids] = 0
    keep_mutations = keep_sites[tables.mutations.site]
    new_ds, new_ds_offset = keep_with_offset(
        keep_mutations,
        tables.mutations.derived_state,
        tables.mutations.derived_state_offset,
    )
    new_md, new_md_offset = keep_with_offset(
        keep_mutations, tables.mutations.metadata, tables.mutations.metadata_offset
    )
    # Mutation numbers will change, so the parent references need altering
    mutation_map = np.cumsum(keep_mutations, dtype=tables.mutations.parent.dtype) - 1
    # Map parent == -1 to -1, and check this has worked (assumes tskit.NULL == -1)
    mutation_map = np.append(mutation_map, -1).astype(tables.mutations.parent.dtype)
    assert mutation_map[tskit.NULL] == tskit.NULL
    tables.mutations.set_columns(
        site=tables.mutations.site[keep_mutations],
        node=tables.mutations.node[keep_mutations],
        time=tables.mutations.time[keep_mutations],
        derived_state=new_ds,
        derived_state_offset=new_ds_offset,
        parent=mutation_map[tables.mutations.parent[keep_mutations]],
        metadata=new_md,
        metadata_offset=new_md_offset,
    )
    if record_provenance:
        # TODO replace with a version of https://github.com/tskit-dev/tskit/pull/243
        parameters = {"command": "delete_site_mutations", "TODO": "add parameters"}
        tables.provenances.add_row(record=json.dumps(get_provenance_dict(parameters)))
    return tables


def combined_ts_constrained_samples(args):
    high_cov_samples = tsinfer.load(args.high_cov)
    dated_hgdp_1kg_sgdp_ts = tskit.load(args.dated_ts)
    sites_time = tsdate.sites_time_from_ts(dated_hgdp_1kg_sgdp_ts)
    dated_samples = tsdate.add_sampledata_times(high_cov_samples, sites_time)
    # Record number of constrained sites
    print("Total number of sites: ", sites_time.shape[0])
    print(
        "Number of ancient lower bounds: ",
        np.sum(high_cov_samples.min_site_times(individuals_only=True) != 0),
    )
    print(
        "Number of corrected times: ", np.sum(dated_samples.sites_time[:] != sites_time)
    )
    high_cov_samples_copy = dated_samples.copy(args.output)
    high_cov_samples_copy.finalise()


def main():

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    subparsers.required = True
    subparsers.dest = "command"

    subparser = subparsers.add_parser("simplify")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("output", type=str, help="Input tree sequence")
    subparser.set_defaults(func=run_simplify)

    subparser = subparsers.add_parser("dated_samples")
    subparser.add_argument("samples", type=str, help="Input sampledata")
    subparser.add_argument("ts", type=str, help="Input dated tree sequence")
    subparser.set_defaults(func=run_get_dated_samples)

    subparser = subparsers.add_parser("sequential-augment")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("--num-threads", type=int, default=0)
    subparser.add_argument("--seed", type=int, default=1)
    subparser.set_defaults(func=run_sequential_augment)

    subparser = subparsers.add_parser("benchmark-tskit")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument(
        "--num-variants",
        type=int,
        default=None,
        help="Number of variants to benchmark genotypes decoding performance on",
    )
    subparser.set_defaults(func=run_benchmark_tskit)

    subparser = subparsers.add_parser("benchmark-vcf")
    subparser.add_argument("input", type=str, help="Input VCF")
    subparser.add_argument(
        "--num-variants",
        type=int,
        default=None,
        help="Number of variants to benchmark genotypes decoding performance on",
    )
    subparser.set_defaults(func=run_benchmark_vcf)

    subparser = subparsers.add_parser("compute-ukbb-gnn")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("output", type=str, help="Filename to write CSV to.")
    subparser.add_argument("--num-threads", type=int, default=16)
    subparser.set_defaults(func=run_compute_ukbb_gnn)

    subparser = subparsers.add_parser("compute-1kg-gnn")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("output", type=str, help="Filename to write CSV to.")
    subparser.add_argument("--num-threads", type=int, default=16)
    subparser.set_defaults(func=run_compute_1kg_gnn)

    subparser = subparsers.add_parser("compute-sgdp-gnn")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("output", type=str, help="Filename to write CSV to.")
    subparser.add_argument("--num-threads", type=int, default=16)
    subparser.set_defaults(func=run_compute_sgdp_gnn)

    subparser = subparsers.add_parser("compute-hgdp-gnn")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("output", type=str, help="Filename to write CSV to.")
    subparser.add_argument("--num-threads", type=int, default=16)
    subparser.set_defaults(func=run_compute_hgdp_gnn)

    subparser = subparsers.add_parser("compute-hgdp-1kg-sgdp-gnn")
    subparser.add_argument("input", type=str, help="Input tree sequence")
    subparser.add_argument("output", type=str, help="Filename to write CSV to.")
    subparser.add_argument("--num-threads", type=int, default=16)
    subparser.set_defaults(func=run_compute_hgdp_1kg_sgdp_gnn)

    subparser = subparsers.add_parser("make-sampledata-compatible")
    subparser.add_argument(
        "--input-sampledata",
        type=str,
        nargs="+",
        help="Input sample files to merge.",
        required=True,
    )
    subparser.set_defaults(func=make_sampledata_compatible)

    subparser = subparsers.add_parser("output-indiv-times")
    subparser.add_argument(
        "input",
        type=str,
        help="Add individuals times to sampledata file.",
    )
    subparser.add_argument(
        "output",
        type=str,
        help="Add individuals times to sampledata file.",
    )
    subparser.set_defaults(func=add_indiv_times)

    subparser = subparsers.add_parser("merge-sampledata-files")
    subparser.add_argument(
        "--input-sampledata",
        type=str,
        nargs="+",
        help="Input sample files to merge.",
        required=True,
    )
    subparser.add_argument("--output", type=str, required=True)
    subparser.set_defaults(func=merge_sampledata_files)

    subparser = subparsers.add_parser("remove-moderns-reich")
    subparser.add_argument(
        "input",
        type=str,
        help="Input reich sampledata file with moderns and ancients.",
    )
    subparser.add_argument("output", type=str, help="Output sampledata file name")
    subparser.set_defaults(func=remove_moderns_reich)

    subparser = subparsers.add_parser("remove-outliers")
    subparser.add_argument("--samples", type=str, help="Sampledata filename")
    subparser.add_argument(
        "--ts", type=str, help="Inferred Tree Sequence.",
    )
    subparser.add_argument(
        "--output-samples", type=str, help="Output sampledata filename"
    )
    subparser.add_argument(
        "--output-ts", type=str, help="Output tree sequence filename"
    )
    subparser.set_defaults(func=remove_outliers)

    subparser = subparsers.add_parser("combined-ts-dated-samples")
    subparser.add_argument(
        "--high-cov",
        type=str,
        help="HGDP + 1kg + SGDP + High-Coverage Ancients.",
    )
    subparser.add_argument(
        "--all-samples",
        type=str,
        help="HGDP + 1kg + SGDP + All Ancients.",
    )
    subparser.add_argument(
        "--dated-ts",
        type=str,
        help="HGDP + 1kg + SGDP Dated Tree Sequence.",
    )
    subparser.add_argument("--output", type=str, help="Output sampledata filename")
    subparser.set_defaults(func=combined_ts_constrained_samples)

    daiquiri.setup(level="INFO")

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
