#!/usr/bin/env python
# -*- coding: utf-8 -*-


import itertools as it, operator as op, functools as ft
from subprocess import Popen, PIPE, STDOUT
from collections import defaultdict
import os, sys, re, types

import argparse
parser = argparse.ArgumentParser(
	description='Apply or check netfilter rules from/against configuration file.')
parser.add_argument('-n', '--no-revert', action='store_true',
	help='Do not schedule tables revert (in case of ssh lock),'
		' not recommended, since you never know when firewall may lock itself up.')
parser.add_argument('-x', '--no-ipsets', action='store_true',
	help='Do not process ipsets and related rules.')
parser.add_argument('-s', '--summary', action='store_true',
	help='Show diff between old and new tables afterwards.')
parser.add_argument('-d', '--dump', action='store_true',
	help='No changes, just dump resulting tables to stdout.')
parser.add_argument('-t', '--check-diff', action='store_true',
	help='No changes, return 0 ("identical") or 2 status (not 1, so it wont be'
		' confused with any generic error), depending on whether there are changes'
		' in configuration waiting to be applied (configuration differs from current'
		' iptables settings). Does not performs any ipset manipulations/comparisons.'
		' It is done in somewhat DANGEROUS way - tables get swapped for a short time.')
parser.add_argument('-c', '--conf',
	default=[
		os.path.splitext(os.path.realpath(__file__))[0]+'.yaml',
		'/etc/trilobite.yaml' ],
	help='Path to configuration file (default: %(default)s).')
parser.add_argument('--debug', action='store_true', help='Verbose operation mode.')
optz = parser.parse_args()

try:
	if not isinstance(optz.conf, types.StringTypes):
		optz.conf = filter(os.path.exists, optz.conf)[0]
	elif not os.path.exists(optz.conf): raise IndexError
except IndexError:
	parser.error('Unable to find configuration file at {}'.format(optz.conf))

import logging
logging.basicConfig( level=logging.INFO
	if not optz.debug else logging.DEBUG )
log = logging.getLogger()

os.umask(077)


builtins = {'input', 'forward', 'output', 'prerouting', 'mangle', 'postrouting'}
extents = {
	'--mac-source': '-m mac',
	'--state': '-m state',
	'--src-range': '-m iprange',
	'--dst-range': '-m iprange',
	'--dport (\S+,)+\S+': '-m multiport',
	'--match-set': '-m set',
	'--pkt-type': '-m pkttype',
	'--uid-owner': '-m owner' }
extents = list( (re.compile('(?<=\s)((! )?'+k+')'), '{} \\1'.format(v))
	for k,v in extents.viewitems() )
pex = re.compile('(?<=-p\s)((\w+/)+\w+)'),\
	re.compile('(?<=port\s)((\d+/)+\d+)') # protocol/port extension
vmark = re.compile('(\s*-(v[46]))(?=\s|$)') # IP version mark


cfg = open(optz.conf).read()
cfg = cfg.replace(r'\t', '  ') # I tend to use tabs, which are not YAML-friendly
cfg = re.sub(re.compile(r'[ \t]*\\\n\s*', re.M), ' ', cfg)


import yaml, yaml.constructor
from collections import OrderedDict

class OrderedDictYAMLLoader(yaml.Loader):
	'Based on: https://gist.github.com/844388'

	def __init__(self, *args, **kwargs):
		yaml.Loader.__init__(self, *args, **kwargs)
		self.add_constructor(u'tag:yaml.org,2002:map', type(self).construct_yaml_map)
		self.add_constructor(u'tag:yaml.org,2002:omap', type(self).construct_yaml_map)

	def construct_yaml_map(self, node):
		data = OrderedDict()
		yield data
		value = self.construct_mapping(node)
		data.update(value)

	def construct_mapping(self, node, deep=False):
		if isinstance(node, yaml.MappingNode):
			self.flatten_mapping(node)
		else:
			raise yaml.constructor.ConstructorError( None, None,
				'expected a mapping node, but found {}'.format(node.id), node.start_mark )

		mapping = OrderedDict()
		for key_node, value_node in node.value:
			key = self.construct_object(key_node, deep=deep)
			try:
				hash(key)
			except TypeError, exc:
				raise yaml.constructor.ConstructorError( 'while constructing a mapping',
					node.start_mark, 'found unacceptable key ({})'.format(exc), key_node.start_mark )
			value = self.construct_object(value_node, deep=deep)
			mapping[key] = value
		return mapping

cfg = yaml.load(cfg, OrderedDictYAMLLoader)


class Tables:
	v4_mark = re.compile('\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}')
	v6_mark = re.compile('[a-f0-9]{0,4}::([a-f0-9]{1,4}|/)') # far from perfect, but should do

	def __init__(self):
		self.rule_counts = dict(v4=defaultdict(int), v6=defaultdict(int))
		self.metrics = set() # table-chain-num-metric
		self.header = dict() # used to skip putting empty comments for omitted (for vX table) rules
		self.chains = dict() # {(vX, table, chain): contents}
		self.policies = dict() # {(vX, table, chain): policy}

	def set_policy(self, table, chain, policy, v=None):
		if isinstance(policy, types.StringTypes): policy = dict(v4=policy, v6=policy)
		elif isinstance(policy, tuple): policy = dict(it.izip(['v4', 'v6'], policy))
		for v in (['v4', 'v6'] if not v else [v]):
			# chain policy must be consistent, hence assert
			try: assert self.policies[v,table,chain] == policy[v]
			except KeyError: self.policies[v,table,chain] = policy[v]

	def append( self, lines, table, chain,
			policy=None, v=None, metrics=None ):
		if not v and lines[0] != '#': # auto-determine if it's valid for each table
			if not self.v6_mark.search(lines): v = 'v4'
			if not self.v4_mark.search(lines):
				v = None if v else 'v6' # empty value means both tables
		for v in (['v4', 'v6'] if not v else [v]):
			if policy: self.set_policy(table, chain, policy, v)
			# Init chain
			try: rules = self.chains[v,table,chain]
			except KeyError: rules = self.chains[v,table,chain] = list()
			# Buffer last header, appending it only if there's a rule following
			if lines[0] == '#': self.header[v,table,chain] = lines
			else:
				try: rules.append(self.header.pop((v,table,chain)))
				except KeyError: pass
				rules.append(lines)
				if chain:
					rule_counts = self.rule_counts[v]
					log.debug('LC-{}: {!r}'.format(v,lines))
					lines_list = filter(None, (lines if isinstance( lines,
						types.StringTypes ) else '\n'.join(lines)).splitlines())
					if metrics:
						if isinstance(metrics, types.StringTypes): metrics = [metrics]
						for i in lines_list:
							rule_counts[table, chain] += 1
							for metric in metrics:
								self.metrics.add((v, table, chain, rule_counts[table, chain], metric))
					else: rule_counts[table, chain] += len(lines_list)
				else: log.debug('L-{}: {!r}'.format(v, lines))

	def fetch(self, v_fetch=None):
		tables = list(
			(k,list(v)) for k,v in it.groupby(
				sorted( self.chains.viewitems(),
					key=lambda ((v,table,chain),contents):\
						(v, table, chain.lower() in builtins, chain) ),
				key=lambda ((v,table,chain),contents): (v, table) ))
		dump = dict()
		for (v, table), chains in tables:
			if v not in dump: dump[v] = list()
			# Chain specs (":INPUT ACCEPT [0:0]")
			chain_headers = list()
			for v, table, chain in it.imap(op.itemgetter(0), chains):
				try: policy = self.policies[v,table,chain]
				except KeyError: continue # no policy - no chain
				chain_headers.append(':{} {} [0:0]'.format(chain, policy.upper()))
			if not chain_headers: continue # no chains - no table
			# Table header (like "*filter")
			dump[v].extend(['### Table: {}'.format(table), '*{}'.format(table), ''])
			dump[v].extend(chain_headers)
			# Actual rules
			for (v, table, chain), contents in chains:
				dump[v].extend(['', '## Chain: {}'.format(chain)])
				dump[v].extend(contents)
			# Final (per-table) "COMMIT" line
			dump[v].extend(['', 'COMMIT', '', ''])
		dump = dict((k, '\n'.join(v)) for k,v in dump.viewitems())
		return op.itemgetter(*(('v4', 'v6') if not v_fetch else [v_fetch]))(dump)

dump = Tables()


def chainspec(chain):
	# Chain policy specification (like'input-lan/-', 'input/6' or 'input/+')
	if '/' in chain: chain, policy = chain.rsplit('/', 1)
	else: policy = cfg['policy']
	if not policy or policy == '-': policy = 'DROP'
	elif policy == 'x': policy = 'REJECT'
	elif policy.isdigit():
		if policy == '4': policy = ('ACCEPT', 'DROP')
		elif policy == '6': policy = ('DROP', 'ACCEPT')
		else: raise ValueError('Incorect policy specification')
	else: policy = 'ACCEPT'

	if '-' in chain: # like 'input-lan', for chain-global interface specification (useful in svc rules)
		if chain.startswith('input'): rule = '-i'
		elif chain.startswith('output'): rule = '-o'
		else: rule, pre = None, list()
		if rule:
			chain, pre = chain.rsplit('-', 1)
			pre = [rule, pre]
	else: pre = list()

	return chain, policy, pre


def diff_summary(old, new):
	from tempfile import NamedTemporaryFile
	with NamedTemporaryFile(prefix='trilobite_old.') as bak_old,\
			NamedTemporaryFile(prefix='trilobite_new.') as bak_new:
		bak_old.write(old+'\n')
		bak_new.write(new+'\n')
		bak_old.flush(), bak_new.flush()
		Popen(cfg['fs']['bin']['diff'].split(' ') + [bak_old.name, bak_new.name]).wait()
	sys.stdout.write('\n\n')



### ipsets

sets = defaultdict(list)
if not optz.no_ipsets and cfg.get('sets'):
	null = open('/dev/null', 'wb')

	# Generate new ipset specs
	for name,props in cfg['sets'].viewitems():
		if optz.check_diff:
			sets[name] = list() # has to exist, nothing more
			continue
		sets[name].append(['-N', name] + props['type'].split())
		for line in (props.get('contents') or list()): sets[name].append(['-A', name] + line.split())

	if not optz.check_diff:

		def pull_sets():
			ipset = Popen([cfg['fs']['bin']['ipset'], '--save'], stdout=PIPE)
			old_sets, stripped = '', list()
			for line in ipset.stdout:
				old_sets += line
				line = line.strip()
				if not line.startswith('#'): stripped.append(line) # strip comments
			if ipset.wait():
				log.fatal('ipsets backup failed, bailing out')
				sys.exit(1)
			return old_sets, '\n'.join(stripped)

		# Pull old sets' configuration
		old_sets, old_essence = pull_sets()

		# Clear namespace for used sets
		for name in list(sets):
			if not Popen([cfg['fs']['bin']['ipset'], '--list', name], stdout=null, stderr=STDOUT).wait():
				if Popen([cfg['fs']['bin']['ipset'], '--destroy', name], stdout=null, stderr=STDOUT).wait():
					log.warn('Failed to destroy ipset "{}", will be skipped on --restore'.format(name))
					sets[name] = list() # should not be restored
		# Push new sets
		ipset = Popen([cfg['fs']['bin']['ipset'], '--restore'], stdin=PIPE)
		ipset.stdin.write('\n'.join(it.imap(' '.join, it.chain.from_iterable(sets.viewvalues()))))
		ipset.stdin.write('\nCOMMIT\n')
		ipset.stdin.close()
		ipset.wait()

		# Pull new sets' configuration, to compare against old ones
		new_sets, new_essence = pull_sets()

		if old_essence != new_essence:
			# Backup old sets in backup.0 slot, rotating the rest of them
			i_slot = None
			for i in sorted(( cfg['fs']['bakz']['sets'].format(num=i)
					for i in xrange(cfg['fs']['bakz']['keep']) ), reverse=True):
				if os.path.exists(i) and i_slot: os.rename(i, i_slot)
				i_slot = i
			else: open(i, 'w').write(old_sets)

			# Generate diff, if requested
			if optz.summary:
				log.info('IPSets:')
				diff_summary(old_essence, new_essence)

			# Schedule sets' revert if no commit action will be issued (to ensure that tables are in the sane state)
			if not optz.no_revert:
				at = Popen([cfg['fs']['bin']['at'], 'now', '+', str(cfg['fs']['bakz']['delay']), 'minutes'], stdin=PIPE)
				for name in sets:
					at.stdin.write('{} --destroy {}\n'.format(cfg['fs']['bin']['ipset'], name)) # destroy modified sets
				at.stdin.write('{} --restore < {}\n'.format(cfg['fs']['bin']['ipset'], i)) # restore from latest backup
				at.stdin.close()
				at.wait()



### iptables

# Used to mark connectons, if metrics_conntrack_chain is set
metrics_mark = 0x1 if cfg.get(
	'metrics_conntrack', dict() ).get('enabled') else None

for table, chainz in cfg['tablez'].viewitems():
	if table == 'nat': table_proto_mark = 'v4'
	else: table_proto_mark = None

	try: svc = chainz.pop('svc')
	except KeyError: svc = dict()

	# Form chainspec / initial rules, giving chains a 'clean', separated from chainspec, names
	for chain in chainz.keys():
		rulez = chainz[chain]
		del chainz[chain]
		chain, policy, pre = chainspec(chain)
		chainz[chain] = policy,\
			[(pre, [rulez] if isinstance(rulez, str) else rulez)] # only policy from the original chain is used

	# Extend chains w/ svc rules, if any
	if svc:
		for name, rulez in svc.viewitems():
			try: pre = rulez.viewitems() # full specification (dict w/ chain and rules list)
			except AttributeError: pre = [('input', rulez)] # it's just a list of rules, defaults to input chain
			for chain, rulez in pre:
				chain, policy, pre = chainspec(chain) # policy here is silently ignored
				rulez = [rulez] if isinstance(rulez, types.StringTypes) else rulez
				chainz[chain][1].append((None, name))
				chainz[chain][1].append((pre, rulez))

	# Sort to extend metrics-chain (if any) with explicit rules
	#  only after appending all the implicit ones from --metrics flags
	chainz = sorted( chainz.viewitems(),
		key=lambda (name, chain): name ==\
			(metrics_mark and cfg[ 'metrics_conntrack']['chain']) )
	# Form actual tables
	for name, chain in chainz:
		policy, ruleset = chain
		if name.lower() in builtins: name = name.upper()
		else: policy = '-' # for custom chains it's always "-"

		dump.set_policy(table, name, policy, v=table_proto_mark)

		for base, rulez in ruleset:
			if rulez:
				if base == None: # comment, no extra processing
					dump.append('# ' + rulez, table, name, v=proto_mark)
					continue

				assert not isinstance(rulez, types.StringTypes)
				for rule in rulez: # rule mangling
					# Rule base: comment / state extension
					if cfg['stateful'] and rule and '--state'\
							not in rule and name == 'INPUT' and '--dport' in rule:
						pre = base + ['--state', 'NEW']
					else: pre = base

					# Check rule for proto marks like "-v4" or "-v6"
					proto_mark = table_proto_mark
					if not proto_mark:
						try: v, proto_mark = vmark.findall(rule)[0]
						except (IndexError, TypeError): proto_mark = None
						else: rule = rule.replace(v, '') # strip the magic

					log.debug('R (IP: {}, table: {}): {!r}'.format(proto_mark, table, rule))
					rule = rule.split() if rule else list()

					# Check for ipset existence
					try: k = rule.index('--match-set')
					except ValueError: ipset = None
					else:
						ipset = rule[k+1]
						if ipset not in sets:
							log.warn('Skipping rule for invalid/unknown ipset "{}"'.format(ipset))
							continue

					# Metrics are split into a separate list
					metrics, metrics_track = list(), False
					for mark in '--metrics-notrack', '--metrics':
						try: k = rule.index(mark)
						except ValueError: pass
						else:
							if mark == '--metrics': metrics_track = True
							metrics.extend(rule[k+1].split('/'))
							rule = rule[:k] + rule[k+2:]

					# Final rules (like '-A INPUT -j DROP')
					if not rule: rule = ['-j', 'DROP']
					elif rule[-1] == 'x': rule = rule[:-1] + ['-j', 'REJECT']
					elif rule[-1] == '-': rule = rule[:-1] + ['-j', 'DROP']
					elif rule[-1] == '<': rule = rule[:-1] + ['-j', 'RETURN']
					elif rule[-1] == '+': rule = rule[:-1] + ['-j', 'ACCEPT']
					elif rule[-1] == '|': rule = rule[:-1] # just a counter or whatever
					elif '-j' not in rule and '-g' not in rule: rule += ['-j', 'ACCEPT']

					if metrics_track and metrics_mark and metrics:
						mark = hex(metrics_mark << cfg['metrics_conntrack']['shift'])
						metrics_mark = metrics_mark << 1 # use unique bits to avoid overriding other marks
						if metrics_mark > 2**32-1:
							raise ValueError('Unable to assign unique connmark bit to each metric')
						k = rule.index('-j')
						# Add CONNMARK rule with the same filter before the original one
						rules = (rule[:k] + [ '-j', 'CONNMARK',
							'--or-mark', mark ], metrics), (rule, None)
						# Add --mark check rule with same metrics to the specified chain
						dump.append(
							' '.join([ '-A', cfg[ 'metrics_conntrack']['chain'],
								'-m', 'connmark', '--mark', '{}/{}'.format(mark, mark) ]),
							table, cfg['metrics_conntrack']['chain'], v=proto_mark,
							metrics=metrics, policy='-' )
					else: rules = [(rule, metrics)]

					for rule, metrics in rules:
						rule = ' '.join(['-A', name] + pre + rule) # rule composition

						for k,v in extents: # rule extension (for example, adds '-m ...', where necessary)
							if v in rule: continue
							rule = k.sub(v, rule)

						# Protocol/port extension (clone rule for each proto/port)
						if rule:
							rules = [rule]
							for ex in pex:
								try:
									rules = list( ex.sub(_ex, rule) for rule in rules
										for _ex in ex.search(rule).groups()[0].split('/') )
								except AttributeError: pass # no matches
							rule = '\n'.join(rules)

						dump.append(rule, table, name, v=proto_mark, metrics=metrics)


# Ignore SIGHUP (in case of SSH break)
import signal
signal.signal(signal.SIGHUP, signal.SIG_IGN) # TODO: add instant-restore as a sighup handler?


def pull_table(v):
	iptables = Popen(cfg['fs']['bin'][v+'_pull'], stdout=PIPE)
	table, stripped = '', list()
	for line in iptables.stdout:
		table += line
		line = line.strip()
		if not (line.startswith('#') or line.startswith(':')):
			stripped.append(line) # strip comments and chains' packet counters
	if iptables.wait():
		log.fatal('iptables ({}) backup failed, bailing out'.format(v))
		sys.exit(1)
	return table, '\n'.join(stripped)

class TableUpdateError(Exception): pass

def push_table(v, table):
	iptables = Popen(cfg['fs']['bin'][v+'_push'], stdin=PIPE)
	iptables.stdin.write(table)
	iptables.stdin.close()
	if iptables.wait(): raise TableUpdateError('Failed to update table')


for v in 'v4', 'v6':
	if not optz.dump:
		# Pull the old table, to check if it's similar to new one (no backup needed in that case)
		old_table, old_essence = pull_table(v)

		# Push new table
		try: push_table(v, dump.fetch(v))
		except TableUpdateError as err: log.error(bytes(err))

		# Pull new table in iptables-save format, to compare against old one
		new_table, new_essence = pull_table(v)

		if old_essence != new_essence:
			if not optz.check_diff:
				# Backup old table in backup.0 slot, rotating the rest of them
				i_slot = None
				for i in sorted(( cfg['fs']['bakz'][v].format(num=i)
						for i in xrange(cfg['fs']['bakz']['keep']) ), reverse=True):
					if os.path.exists(i) and i_slot: os.rename(i, i_slot)
					i_slot = i
				else: open(i, 'w').write(old_table)
			else:
				push_table(v, old_table) # restore old table

			# Generate diff, if requested
			if optz.summary:
				log.info('{} table:'.format(v))
				diff_summary(old_essence, new_essence)

			# First diff means we're done if that's what is requested
			if optz.check_diff: sys.exit(2)

			# Schedule table revert if no commit action will be issued (to ensure that tables are in the sane state)
			if not optz.no_revert:
				at = Popen([cfg['fs']['bin']['at'], 'now', '+', str(cfg['fs']['bakz']['delay']), 'minutes'], stdin=PIPE)
				at.stdin.write('{} < {}\n'.format(cfg['fs']['bin'][v+'_push'], i)) # restore from latest backup
				at.stdin.close()
				at.wait()

	else:
		log.info('{} table:'.format(v))
		sys.stdout.write(dump.fetch(v)+'\n\n')


if dump.metrics:
	metric_repr = lambda metric: ' '.join(it.imap(bytes, metric))

	if optz.dump:
		log.info('Metrics:')
		metrics_dump = '\n'.join(it.imap(metric_repr, sorted(dump.metrics))) + '\n'
		sys.stdout.write(metrics_dump)

	else:
		metrics = dict()
		for line in sorted(dump.metrics):
			metrics.setdefault(line[0], list()).append(line[1:])
		for v, metrics in metrics.viewitems():
			try: dst = cfg['fs']['metrics'][v]
			except KeyError: continue
			metrics_dump = '\n'.join(it.imap(metric_repr, sorted(metrics))) + '\n'
			open(dst, 'wb').write(metrics_dump)
