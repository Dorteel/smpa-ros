"""Mocked conversation modelling contract, independent of model weights and ROS."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen
from unittest.mock import Mock

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, DCTERMS, OWL, RDFS

from smpa_ros.belief import ontology_graph, read_graph, validate_triples, graph_view, triple_schema
from smpa_ros.conversation import exchange, open_conversation, read_messages
from smpa_ros.conversation_web import make_server


class BeliefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = open_conversation(Path(self.temp.name))
        self.ns = 'urn:smpa:' + self.memory.metadata['run_id'] + ':'
        self.vocab = 'urn:smpa:belief#'
        self.ontology, self.digest = ontology_graph()

    def triples(self, text='The cup is red.', suffix='claim'):
        return [dict(s=self.ns+'cup', p=str(RDF.type), o={'uri':self.vocab+'Entity'}),
                dict(s=self.ns+suffix, p=str(RDF.type), o={'uri':self.vocab+'Proposition'}),
                dict(s=self.ns+suffix, p=self.vocab+'text', o={'literal':text}),
                dict(s=self.ns+suffix, p=self.vocab+'about', o={'uri':self.ns+'cup'})]

    def model(self, sds='The cup is red.', rows=None):
        def generate(history, **settings):
            if '"sds"' in history[0]['content']:
                return {'response':json.dumps({'sds':sds}), 'model_id':'mock-model'}
            return {'response':json.dumps({'triples':rows if rows is not None else self.triples(sds)}), 'model_id':'mock-model'}
        return dict(generate=Mock(side_effect=generate), settings={}, agent={'provider':'mock', 'model_id':'requested'})

    def test_sds_attribution_nesting_negation_and_resume(self):
        text='The doctor believes the following:\n  The patient will not necessarily recover.'
        model=self.model(text)
        answer=Mock(return_value='I understand.')
        exchange(self.memory, text, answer, {}, modelling=model)
        sds=(self.memory.directory/'conversation-sds.md').read_text()
        self.assertIn(text,sds)
        self.assertIn('## User — message 1',sds)
        self.assertIn('## Assistant — message 2',sds)
        self.assertIn('conversation.md#message-1',sds)
        self.assertIn('conversation.md#message-2',sds)
        self.assertEqual(answer.call_args.args[0][0]['role'],'system')
        self.assertIn('urn:smpa:belief',answer.call_args.args[0][0]['content'])
        before=read_graph(self.memory.directory)
        resumed=open_conversation(None,resume=self.memory.directory)
        exchange(resumed,'Continue.',answer,{},modelling=model)
        self.assertTrue((resumed.directory/'conversation-sds.md').read_text().startswith(sds))
        self.assertTrue(set(before)<=set(read_graph(resumed.directory)))
        self.assertEqual(len(read_messages(resumed.directory)),4)
        self.assertEqual(resumed.metadata['belief_ontology_sha256'],self.digest)
        records=sorted((resumed.directory/'state').glob('*/model*.json'))
        self.assertTrue(all(json.loads(p.read_text())['status']=='succeeded' for p in records))
        self.assertEqual(json.loads(records[0].read_text())['outputs'][0]['model_id'],'mock-model')
        self.assertEqual(json.loads(records[0].read_text())['outputs'][0]['source'],'conversation.md#message-1')

    def test_vocabulary_reference_and_structure_validation(self):
        self.ontology.add((OWL.Class, RDF.type, OWL.Class))
        self.ontology.add((RDFS.subClassOf, RDF.type, OWL.ObjectProperty))
        bad=[dict(s=self.ns+'x',p=self.vocab+'unknown',o={'literal':'x'})]
        with self.assertRaisesRegex(ValueError,'Undeclared property'):
            validate_triples(bad,self.ontology,Graph(),self.ns,'source')
        for rows in ([dict(s=self.ns+'newClass',p=str(RDF.type),o={'uri':str(OWL.Class)})],
                     self.triples()+[dict(s=self.ns+'cup',p=str(RDFS.subClassOf),o={'uri':self.ns+'cup'})],
                     self.triples()+[dict(s=self.ns+'cup',p=str(DCTERMS.source),o={'literal':'invented source'})],
                     [dict(s=self.ns+'x',p=str(RDF.type),o={'uri':self.vocab+'Invented'})],
                     self.triples()+[dict(s=self.ns+'claim',p=self.vocab+'about',o={'uri':self.ns+'missing'})],
                     [dict(s=self.ns+'x',p=self.vocab+'text',o={'uri':self.ns+'x'})],
                     [dict(s=self.vocab+'Entity',p=str(RDF.type),o={'uri':str(RDF.Property)})],
                     [dict(s=self.ns+'x',p=self.vocab+'text',o={'literal':'untyped'})]):
            with self.assertRaises(ValueError):
                validate_triples(rows,self.ontology,Graph(),self.ns,'source')

    def test_nested_belief_remains_scoped_and_coarse(self):
        b, n = self.vocab, self.ns
        rows = [dict(s=n+node, p=str(RDF.type), o={'uri':b+kind})
                for node, kind in [('doctor','Entity'), ('belief','Belief'), ('recovery','Proposition')]]
        rows += [dict(s=n+'belief', p=b+'attributedTo', o={'uri':n+'doctor'}),
                 dict(s=n+'recovery', p=b+'withinScope', o={'uri':n+'belief'}),
                 dict(s=n+'recovery', p=b+'text', o={'literal':'The patient will not necessarily recover.'})]
        model = self.model('The doctor believes the following:\n  The patient will not necessarily recover.', rows)
        exchange(self.memory, 'The doctor believes that the patient will not necessarily recover.',
                 Mock(return_value='That is the doctor’s belief.'), {}, modelling=model)
        graph = read_graph(self.memory.directory)
        self.assertIn((URIRef(n+'recovery'), URIRef(b+'withinScope'), URIRef(n+'belief')), graph)
        self.assertIn((URIRef(n+'belief'), URIRef(b+'attributedTo'), URIRef(n+'doctor')), graph)
        self.assertFalse(any(str(predicate).endswith('willRecover') for _, predicate, _ in graph))
        self.assertEqual(ontology_graph()[1], self.digest)

    def test_schema_uses_ontology_and_compact_identifiers(self):
        from jsonschema import validate, ValidationError
        schema = triple_schema(self.ontology)
        rows = [dict(s='#cup', p=str(RDF.type), o={'uri':self.vocab+'Entity'})]
        validate({'triples':rows}, schema)
        graph = validate_triples(rows, self.ontology, Graph(), self.ns, 'source')
        self.assertIn((URIRef(self.ns+'cup'), RDF.type, URIRef(self.vocab+'Entity')), graph)
        for predicate, obj in [('rdfs:type', {'uri':self.vocab+'Entity'}),
                               (self.vocab+'about', {'literal':'red'})]:
            with self.assertRaises(ValidationError):
                validate({'triples':[dict(s='#cup', p=predicate, o=obj)]}, schema)
        model = self.model(rows=rows)
        model['agent']['backend'] = 'transformers'
        exchange(self.memory, 'cup', Mock(return_value='yes'), {}, modelling=model)
        self.assertEqual(model['generate'].call_args.kwargs['schema'], schema)

    def test_incremental_reuse_and_provenance(self):
        first=validate_triples(self.triples(),self.ontology,Graph(),self.ns,'conversation.md#user')
        second=validate_triples(self.triples('The cup belongs to John.','ownership'),self.ontology,first,self.ns,'conversation.md#user-1')
        self.assertTrue(set(first)<=set(second))
        self.assertEqual(len(list(second.subjects(RDF.type,URIRef(self.vocab+'Entity')))),1)
        self.assertEqual(len(list(second.objects(URIRef(self.ns+'cup'),DCTERMS.source))),2)

    def test_alternative_ontology_and_no_modification(self):
        path=Path(self.temp.name)/'alternative.owl'
        path.write_text('''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:owl="http://www.w3.org/2002/07/owl#"><owl:Class rdf:about="urn:alternative:Thing"/><owl:DatatypeProperty rdf:about="urn:alternative:description"/></rdf:RDF>''')
        before=path.read_bytes()
        ontology,digest=ontology_graph(path)
        rows=[dict(s=self.ns+'x',p=str(RDF.type),o={'uri':'urn:alternative:Thing'}),
              dict(s=self.ns+'x',p='urn:alternative:description',o={'literal':'Uncertain ownership; general concept only.'})]
        model=self.model(rows=rows);model['ontology']=path
        exchange(self.memory,'Perhaps John owns it.',Mock(return_value='Perhaps.'),{},modelling=model)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(digest,hashlib.sha256(before).hexdigest())
        self.assertIn((URIRef(self.ns+'x'),RDF.type,URIRef('urn:alternative:Thing')),read_graph(self.memory.directory))
        prompt=json.loads(model['generate'].call_args.args[0][1]['content'])
        self.assertIn('urn:alternative:',prompt['ontology'])
        path.write_bytes(before.replace(b'Thing',b'Other'))
        exchange(self.memory,'Again',Mock(return_value='Still chatting'),{},modelling=model)
        self.assertIn('modelling_error',self.memory.metadata)

    def test_failed_update_keeps_graph_and_chat(self):
        exchange(self.memory,'cup',Mock(return_value='OK'),{},modelling=self.model())
        previous=(self.memory.directory/'belief-state.ttl').read_bytes()
        exchange(self.memory,'bad update',Mock(return_value='Conversation continues'),{},modelling=self.model(rows=[]))
        self.assertEqual((self.memory.directory/'belief-state.ttl').read_bytes(),previous)
        self.assertIn('modelling_error',self.memory.metadata)
        self.assertEqual(read_messages(self.memory.directory)[-1]['content'],'Conversation continues')
        self.assertIn('message 3',(self.memory.directory/'conversation-sds.md').read_text())
        broken=self.model();broken['generate']=Mock(side_effect=RuntimeError('unavailable'))
        exchange(self.memory,'still here',Mock(return_value='yes'),{},modelling=broken)
        self.assertEqual(read_messages(self.memory.directory)[-1]['content'],'yes')

    def test_one_validated_repair_keeps_rejected_output(self):
        model = self.model()
        original = model['generate'].side_effect
        def generate(history, **settings):
            if history[0]['content'].startswith('Return one JSON'):
                return {'response':json.dumps({'triples':self.triples()[1:]})}
            if history[0]['content'].startswith('Repair'):
                return {'response':json.dumps({'triples':self.triples()})}
            return original(history, **settings)
        model['generate'].side_effect = generate
        exchange(self.memory, 'cup', Mock(return_value='OK'), {}, modelling=model)
        record = json.loads((self.memory.directory/'state/cycle_000001/model.json').read_text())
        self.assertEqual(record['status'], 'succeeded')
        self.assertEqual(len(record['validation_errors']), 1)
        self.assertEqual(len(record['outputs']), 3)
        self.assertTrue(read_graph(self.memory.directory))

    def test_http_graph_sds_and_legacy_resume(self):
        exchange(self.memory,'cup',Mock(return_value='OK'),{},modelling=self.model())
        server=make_server(self.memory,Mock(return_value='hello'),{},port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urlopen(base+'/api/graph') as response:
                state=json.load(response)
            self.assertTrue(state['graph']['nodes'])
            self.assertIn('cup', [node['label'] for node in state['graph']['nodes']])
            self.assertTrue(state['graph']['edges'])
            self.assertTrue(any('source' in p['property'] for n in state['graph']['nodes'] for p in n['properties']))
            with urlopen(base+'/api/sds') as response:
                self.assertIn(b'## User',response.read())
        finally:
            server.shutdown();server.server_close();thread.join()
        legacy=open_conversation(Path(self.temp.name))
        (legacy.directory/'belief-state.ttl').unlink()
        (legacy.directory/'conversation-sds.md').unlink()
        resumed=open_conversation(None,resume=legacy.directory)
        self.assertEqual(graph_view(resumed.directory),{'nodes':[],'edges':[]})
        exchange(resumed,'legacy',Mock(return_value='yes'),{},modelling=self.model())
        self.assertEqual(read_messages(resumed.directory)[-1]['content'],'yes')
