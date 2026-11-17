# BFO: BFO 2020 Core

Unmodified RDF/XML OWL distribution from the BFO developer repository, retaining
`21838-2/owl/bfo-core.owl` and all embedded contributor and ontology metadata.

- Distribution: https://github.com/BFO-ontology/BFO-2020
- License: Creative Commons Attribution 4.0 International (CC BY 4.0), as stated
  in the ontology and the upstream README's License section:
  https://github.com/BFO-ontology/BFO-2020#license
- Attribution: BFO contributors, named individually in the ontology.
- Limitations: this is the OWL Core distribution, not the complete Common Logic
  axiomatization or optional temporal-extension profiles. Those alternatives
  are not dependencies of this entry point and are not included.

`config.yaml` pins the source commit and byte checksum; `main` is relative to
this directory. Local RDF/XML parsing succeeds and declares no `owl:imports`;
there are no unresolved imports. Parsing is not OWL profile, consistency or
reasoning validation. No loading or runtime integration is implemented here.
