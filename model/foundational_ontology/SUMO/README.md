# SUMO: documented 2010 OWL translation

Unmodified `SUMO.owl` from the explicitly approved, commit-pinned mirror:
https://github.com/abrahaj/SUMO.owl/tree/b7e227b4f0d0b4395c5e1bcb26872c492b41823e

The embedded header identifies a provisional, necessarily lossy OWL translation
produced on Sun May 09 18:37:01 PDT 2010. It points to Ontology Portal's SUO-KIF
as authoritative. The publisher documents the OWL translation at
https://www.ontologyportal.org/ (historical download:
http://www.ontologyportal.org/SUMO.owl).

Publisher HTTPS downloads returned HTTP 403 during acquisition. This is a mirror,
not a publisher-verified snapshot; its bytes could not be compared against the
publisher's copy. `config.yaml` records the actual download URL, mirror commit,
original filename and SHA-256. No content has been repaired or regenerated.

## License and attribution

The embedded notice and mirror README state GNU Public License, without specifying
a version. Do not assume a particular GPL version or the enclosing repository's
license. The embedded notice credits YAGO mappings and WordNet content; their
attribution is retained verbatim. No separate license text was supplied by the
mirror. The snapshot's historical provenance and unspecified GPL version are
limitations when assessing redistribution or reuse.

## Parsing and limitations

`main` is relative to this directory. The original RDF/XML contains relative
IRIs and no `xml:base`. Preserve its published document IRI when parsing:

```python
from rdflib import Graph
ontology = Graph().parse("model/foundational_ontology/SUMO/SUMO.owl",
                         format="xml",
                         publicID="http://www.ontologyportal.org/SUMO.owl")
```

This base is also recorded as `document_iri`; parsing using only a local path
would resolve relative identifiers against the filesystem and change their
meaning. The resulting ontology IRI is `http://www.ontologyportal.org/SUMO`.
Local RDF/XML parsing succeeds with 587,998 triples and no `owl:imports`, so no
imports remain unresolved. WordNet/YAGO references and other outbound links are
not import declarations; their external datasets are not bundled or dereferenced.

The OWL translation is old and loses SUO-KIF expressiveness. Parsing does not
establish OWL DL conformance, logical consistency or equivalence to current SUMO.
No reasoning, mappings, import resolver or runtime integration is added here.
