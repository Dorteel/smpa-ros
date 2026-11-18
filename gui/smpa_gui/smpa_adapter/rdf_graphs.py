"""Read an RDF Turtle file and describe its triples as a generic graph for drawing.

Concepts:
    A resource is an IRI or blank node that appears as subject or object.
    An edge is a triple whose object is a resource. A literal (text, number,
    date) is attached to its subject as a value, not drawn as a node, unless
    the viewer asks for that. Labels are the IRI shortened with the file's
    own prefixes. No vocabulary is interpreted: ``rdf:type``, ``rdfs:label``
    and every other predicate are treated exactly alike. This keeps drawing
    independent of graph semantics, which belong to SMPA.

    The state of a file is one of: ``ok``, ``empty`` (exists, holds no
    triples), ``missing`` (does not exist), ``malformed`` (not valid Turtle;
    often a writer is midway through), ``unavailable`` (cannot be read, for
    example no permission). The revision is a digest of the file content, so
    a viewer redraws only when the file really changed.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

MAX_TRIPLES = 5000


def term_id(term):
    """Return a stable id for a resource: its IRI, or ``_:`` plus the blank node label."""
    from rdflib import BNode
    return '_:' + str(term) if isinstance(term, BNode) else str(term)


def label_for(graph, term):
    """Shorten an IRI with the file's prefixes; fall back to the part after the last # or /."""
    from rdflib import BNode
    if isinstance(term, BNode):
        return '_:' + str(term)[:8]
    try:
        prefix, namespace, local = graph.namespace_manager.compute_qname(str(term), generate=False)
        return f'{prefix}:{local}' if prefix else local
    except Exception:
        text = str(term)
        for separator in ('#', '/', ':'):
            if separator in text.rstrip(separator):
                return text.rstrip(separator).rsplit(separator, 1)[1] or text
        return text


def empty_result(state, path, **fields):
    """Describe a file that cannot be drawn, in the same shape as a drawable one."""
    result = dict(state=state, path=str(path), revision=None, triples=0, nodes=[], edges=[], prefixes={},
                  truncated=False, error=None, modified_at=None, size=None)
    result.update(fields)
    return result


def read(path):
    """Read one Turtle file and return its state and, when readable, its nodes and edges.

    The file is read once into memory, so a writer replacing it meanwhile
    cannot mix two versions. More than ``MAX_TRIPLES`` triples are cut and
    reported as truncated. Parsing errors keep the parser's message.
    """
    from rdflib import Graph, Literal
    path = Path(path)
    try:
        info = path.stat()
        if not path.is_file():
            return empty_result('unavailable', path, error='Not a regular file')
        data = path.read_bytes()
    except FileNotFoundError:
        return empty_result('missing', path)
    except OSError as error:
        return empty_result('unavailable', path, error=f'{type(error).__name__}: {error.strerror or error}')
    revision = hashlib.sha1(data).hexdigest()[:16]
    modified = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
    graph = Graph()
    try:
        graph.parse(data=data.decode('utf-8'), format='turtle')
    except Exception as error:
        return empty_result('malformed', path, revision=revision, modified_at=modified, size=len(data),
                            error=f'{type(error).__name__}: {str(error)[:400]}')
    if len(graph) == 0:
        return empty_result('empty', path, revision=revision, modified_at=modified, size=len(data))
    triples = sorted(graph, key=lambda triple: tuple(map(str, triple)))
    truncated = len(triples) > MAX_TRIPLES
    nodes, edges = {}, []

    def node(term):
        """Return the node entry for a resource, creating it on first sight."""
        key = term_id(term)
        if key not in nodes:
            nodes[key] = dict(id=key, label=label_for(graph, term), iri=str(term), literals=[],
                              blank=key.startswith('_:'))
        return nodes[key]

    for subject, predicate, value in triples[:MAX_TRIPLES]:
        source = node(subject)
        if isinstance(value, Literal):
            source['literals'].append(dict(predicate=str(predicate), predicate_label=label_for(graph, predicate),
                                           value=str(value), datatype=str(value.datatype) if value.datatype else None,
                                           language=value.language))
        else:
            target = node(value)
            edges.append(dict(id=f'{source["id"]}|{predicate}|{target["id"]}', source=source['id'], target=target['id'],
                              predicate=str(predicate), label=label_for(graph, predicate)))
    prefixes = {prefix: str(namespace) for prefix, namespace in graph.namespace_manager.namespaces()
                if prefix and str(namespace) in data.decode('utf-8', errors='ignore')}
    return dict(state='ok', path=str(path), revision=revision, triples=len(triples), nodes=sorted(nodes.values(), key=lambda item: item['id']),
                edges=edges, prefixes=prefixes, truncated=truncated, error=None, modified_at=modified, size=len(data))
