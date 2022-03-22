import os
import math
import json
import time
import logging
import requests
import threading
from queue import Queue
from copy import deepcopy

import streamlit as st
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import pandas as pd
from tqdm import tqdm
from pygments import highlight
from pygments.lexers.data import JsonLexer
from pygments.formatters.terminal import TerminalFormatter

requests.packages.urllib3.disable_warnings()

LOG = logging.getLogger(__name__)

class IntraAPIClient(object):
	verify_requests = False

	def __init__(self, progress_bar=False):
		self.client_id = os.environ['client']
		self.client_secret = os.environ['secret']
		self.token_url = os.environ['uri']
		self.api_url = os.environ['endpoint']
		self.scopes = ""
		self.progress_bar = progress_bar
		self.token = None

	def request_token(self):
		request_token_payload = {
			"client_id": self.client_id,
			"client_secret": self.client_secret,
			"grant_type": "client_credentials",
			"scope": self.scopes,
		}
		LOG.debug("Attempting to get a token from intranet")
		self.token = "token_dummy"
		res = self.request(requests.post, self.token_url, params=request_token_payload)
		rj = res.json()
		self.token = rj["access_token"]
		LOG.info(f"Got new acces token from intranet {self.token}")

	def _make_authed_header(self, header={}):
		ret = {"Authorization": f"Bearer {self.token}"}
		ret.update(header)
		return ret

	def request(self, method, url, headers={}, **kwargs):
		if not self.token:
			self.request_token()
		tries = 0
		if not url.startswith("http"):
			url = f"{self.api_url}/{url}"

		while True:
			LOG.debug(f"Attempting a request to {url}")

			res = method(
				url,
				headers=self._make_authed_header(headers),
				verify=self.verify_requests,
				**kwargs
			)

			rc = res.status_code
			if rc == 401:
				if 'www-authenticate' in res.headers:
					_, desc = res.headers['www-authenticate'].split('error_description="')
					desc, _ = desc.split('"')
					if desc == "The access token expired" or desc == "The access token is invalid":
						if self.token != "token_dummy":
							LOG.warning(f"Server said our token {self.token} {desc.split(' ')[-1]}")
						if tries < 5:
							LOG.debug("Renewing token")
							tries += 1
							self.request_token()
							continue
						else:
							LOG.error("Tried to renew token too many times, something's wrong")

			if rc == 429:
				LOG.info(f"Rate limit exceeded - Waiting {res.headers['Retry-After']}s before requesting again")
				time.sleep(float(res.headers['Retry-After']))
				continue

			if rc >= 400:
				req_data = "{}{}".format(url, "\n" + str(kwargs['params']) if 'params' in kwargs.keys() else "")
				if rc < 500:
					raise ValueError(f"\n{res.headers}\n\nClientError. Error {str(rc)}\n{str(res.content)}\n{req_data}")
				else:
					raise ValueError(f"\n{res.headers}\n\nServerError. Error {str(rc)}\n{str(res.content)}\n{req_data}")

			LOG.debug(f"Request to {url} returned with code {rc}")
			return res

	def get(self, url, headers={}, **kwargs):
		return self.request(requests.get, url, headers, **kwargs)

	def post(self, url, headers={}, **kwargs):
		return self.request(requests.post, url, headers, **kwargs)

	def patch(self, url, headers={}, **kwargs):
		return self.request(requests.patch, url, headers, **kwargs)

	def put(self, url, headers={}, **kwargs):
		return self.request(requests.put, url, headers, **kwargs)

	def delete(self, url, headers={}, **kwargs):
		return self.request(requests.delete, url, headers, **kwargs)

	def pages(self, url, headers={}, **kwargs):
		kwargs['params'] = kwargs.get('params', {}).copy()
		kwargs['params']['page'] = int(kwargs['params'].get('page', 1))
		kwargs['params']['per_page'] = kwargs['params'].get('per_page', 100)
		data = self.get(url=url, headers=headers, **kwargs)
		total = data.json()
		if 'X-Total' not in data.headers:
			return total
		last_page = math.ceil(int(data.headers['X-Total']) /
			int(data.headers['X-Per-Page']))
		for page in tqdm(range(kwargs['params']['page'], last_page),
			initial=1, total=last_page - kwargs['params']['page'] + 1,
			desc=url, unit='p', disable=not self.progress_bar):
			kwargs['params']['page'] = page + 1
			total += self.get(url=url, headers=headers, **kwargs).json()
		return total


	def pages_threaded(self, url, headers={}, threads=20, stop_page=None,
															thread_timeout=15, **kwargs):
		def _page_thread(url, headers, queue, **kwargs):
			queue.put(self.get(url=url, headers=headers, **kwargs).json())

		kwargs['params'] = kwargs.get('params', {}).copy()
		kwargs['params']['page'] = int(kwargs['params'].get('page', 1))
		kwargs['params']['per_page'] = kwargs['params'].get('per_page', 100)

		data = self.get(url=url, headers=headers, **kwargs)
		total = data.json()

		if 'X-Total' not in data.headers:
			return total

		last_page = math.ceil(
			float(data.headers['X-Total']) / float(data.headers['X-Per-Page'])
		)
		last_page = stop_page if stop_page and stop_page < last_page else last_page
		page = kwargs['params']['page'] + 1
		pbar = tqdm(initial=1, total=last_page - page + 2,
			desc=url, unit='p', disable=not self.progress_bar)

		while page <= last_page:
			active_threads = []
			for _ in range(threads):
				if page > last_page:
					break
				queue = Queue()
				kwargs['params']['page'] = page
				at = threading.Thread(target=_page_thread,
					args=(url, headers, queue), kwargs=deepcopy(kwargs))

				at.start()
				active_threads.append({
					'thread': at,
					'page': page,
					'queue': queue
					})
				page += 1

			for th in range(len(active_threads)):
				active_threads[th]['thread'].join(timeout=threads * thread_timeout)
				if active_threads[th]['thread'].is_alive():
					raise RuntimeError(f'Thread timeout after waiting for {threads * thread_timeout} seconds')
				total += active_threads[th]['queue'].get()
				pbar.update(1)

		pbar.close()
		return total

	def progress_disable(self):
		self.progress_bar = False

	def progress_enable(self):
		self.progress_bar = True

	def prompt(self):
		while 42:
			qr = input("$> http://api.intra.42.fr/v2/")

			if qr == "token":
				print(ic.token)
				continue

			try:
				ret = ic.get(qr)
				json_str = json.dumps(ret.json(), indent=4)
				print(highlight(json_str, JsonLexer(), TerminalFormatter()))
			except Exception as e:
				print(e)

ic = IntraAPIClient()

def page_config():
	st.set_page_config(layout = 'wide', page_title = "42 Gantt", page_icon = "🤖")

def getUserID(api, login):
	try:
		user = api.pages_threaded(f"https://api.intra.42.fr/v2/users/{login}")
		if user:
			try:
				user_id = user.get('id')
				user_name = user.get('first_name')
			except:
				user_id = 0
				user_name = "Nobody"
		else:
			user_id = 0
			user_name = "Nobody"
		#print(f"getUserID() returns: {user_id}")
	except:
		user_id = 0
		user_name = "Nobody"
	return user_id, user_name

def getBethansProjects():
	projects = pd.DataFrame({
		'cursus_ids': [21, 21, 21, 21],
		'project': [
			{'name': "Being awesome"},
			{'name': "Being incredible"},
			{'name': "Being fabulous"},
			{'name': "Being astonishing"}
			],
		'created_at': [
			pd.Timestamp.now().tz_localize(tz='UTC') - pd.Timedelta(weeks = 999),
			pd.Timestamp.now().tz_localize(tz='UTC') - pd.Timedelta(weeks = 999),
			pd.Timestamp.now().tz_localize(tz='UTC') - pd.Timedelta(weeks = 999),
			pd.Timestamp.now().tz_localize(tz='UTC') - pd.Timedelta(weeks = 999)
			],
		'updated_at': [
			pd.Timestamp.now().tz_localize(tz='UTC'),
			pd.Timestamp.now().tz_localize(tz='UTC'),
			pd.Timestamp.now().tz_localize(tz='UTC'),
			pd.Timestamp.now().tz_localize(tz='UTC')
			],
		'teams': [{}, {}, {}, {}],
		'status': ['finished', 'finished', 'finished', 'finished']
	})
	return projects

def getProjects(api, user_id):
	try:
		list_projects = api.pages_threaded(f"/users/{user_id}/projects_users")
	except:
		st.error("An error occurred when fetching project data. Please try again in a few minutes.")
		projects = pd.DataFrame()
		return projects
	if list_projects:
		projects = pd.DataFrame(list_projects)
		if (projects.shape[0] == 0):
			st.warning("Given user has not worked on any project!")
		#print(f"getProjects() returns a pd.DataFrame[{projects.shape[0]},{projects.shape[1]}]")
		#st.dataframe(projects)
		for i in range(projects.shape[0]):
			try:
				projects.loc[i, 'cursus_ids'] = projects.cursus_ids.values[i][0]
			except:
				projects.loc[i, 'cursus_ids'] = 0
	if (user_id == 95944):
		projects = getBethansProjects()
	return projects

def getCoreProjects(projects):
	core_projects = projects[projects['cursus_ids'] == 21]
	core_projects = core_projects.reset_index()
	if core_projects.shape[0] == 0:
		st.warning("Given user has not worked on any core project!")
	#print(f"getCoreProjects() returns a pd.DataFrame[{core_projects.shape[0]},{core_projects.shape[1]}]")
	for i in range(core_projects.shape[0]):
		core_projects.loc[i, 'project'] = core_projects.project[i].get('name')
	return core_projects

def getToday():
	today = pd.Timestamp.now()
	today = today.tz_localize(tz='UTC')
	return today

def squishCPP(core_projects, today):
	try:
		cpp08EndDate = core_projects.loc[core_projects.project == "CPP Module 08"][["updated_at"]].values[0][0]
		core_projects.at[core_projects.loc[core_projects.project == "CPP Module 00"].index[0], "updated_at"] = cpp08EndDate
	except:
		print("CPP Module 08 not found")
		try:
			core_projects.at[core_projects.loc[core_projects.project == "CPP Module 00"].index[0], "updated_at"] = today
		except:
			print("CPP Module 00 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 08")].index[0]])
	except:
		print("CPP Module 08 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 07")].index[0]])
	except:
		print("CPP Module 07 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 06")].index[0]])
	except:
		print("CPP Module 06 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 05")].index[0]])
	except:
		print("CPP Module 05 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 04")].index[0]])
	except:
		print("CPP Module 04 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 03")].index[0]])
	except:
		print("CPP Module 03 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 02")].index[0]])
	except:
		print("CPP Module 02 not found")
	try:
		core_projects = core_projects.drop([core_projects.loc[core_projects.project == ("CPP Module 01")].index[0]])
	except:
		print("CPP Module 01 not found")
	try:
		core_projects.loc[core_projects.loc[core_projects.project == "CPP Module 00"].index[0], 'project'] = "C++"
	except:
		print("CPP Module 00 not found")
	core_projects = core_projects.reset_index()
	return core_projects

def colorPicker(colors, project):
	if (project in ['Libft', 'get_next_line', 'ft_printf', 'push_swap', "ft_containers"] or "CPP" in project): # algorithm
		return colors[0]
	elif (project in ['FdF', 'fract-ol', 'so_long', 'cub3d', 'miniRT']): # graphical
		return colors[3]
	elif (project in ['pipex', 'minitalk', 'minishell', 'Philosophers', 'webserv', 'ft_irc']): # unix
		return colors[4]
	elif (project in ['Born2beroot' ,'Inception', 'NetPractice']): # sysadmin
		return colors[1]
	elif (project in ['ft_transcendence']): # web
		return colors[6]
	elif ("Exam" in project):
		return colors[7]
	elif ("Being" in project):
		return colors[2]

def sumStudyHours(core_projects):
	sumHours = 0
	for project in core_projects.project.to_list():
		if project == 'Libft':
			sumHours += 70
		elif project == 'get_next_line':
			sumHours += 70
		elif project == 'ft_printf':
			sumHours += 175
		elif project == 'Born2beroot':
			sumHours += 40
		elif project == 'push_swap':
			sumHours += 60
		elif project == 'ft_containers':
			sumHours += 140
		elif project == 'C++':
			sumHours += 7 * 9
		elif project == 'FdF':
			sumHours += 60
		elif project == 'fract-ol':
			sumHours += 60
		elif project == 'so_long':
			sumHours += 60
		elif project == 'cub3d':
			sumHours += 280
		elif project == 'miniRT':
			sumHours += 280
		elif project == 'pipex':
			sumHours += 50
		elif project == 'minitalk':
			sumHours += 50
		elif project == 'minishell':
			sumHours += 210
		elif project == 'Philosophers':
			sumHours += 70
		elif project == 'webserv':
			sumHours += 180
		elif project == 'ft_irc':
			sumHours += 180
		elif project == 'ft_transcendence':
			sumHours += 250
		elif ("Being" in project):
			sumHours += 9999
			return sumHours
		
	return sumHours

def buildGantt(figureW, figureH, core_projects, today):
	plt.rcParams["font.family"] = "sans-serif"
	plt.rcParams['font.size'] = 20
	plt.rcParams['figure.dpi'] = 1200
	fig, gnt = plt.subplots(figsize = (figureW, figureH))
	totalProjects = core_projects.shape[0]
	basePosY = 20
	barWidth = 16
	if (totalProjects > 0):
		startDate = pd.Timestamp(core_projects.updated_at[totalProjects - 1]) - pd.Timedelta(days = 30)
	else:
		startDate = pd.Timestamp(today) - pd.Timedelta(days = 180)
	endDate = pd.Timestamp(today) + pd.Timedelta(days = 30)
	gnt.set_xlim(startDate, endDate)
	gnt.grid(linestyle = 'dotted', linewidth = 1, axis = 'x')
	gnt.set_axisbelow(True)
	gnt.set_yticks(list(range(20 * (totalProjects), 0, -20)))
	gnt.set_yticklabels(core_projects.project.to_list(), fontsize = 14)
	gnt.axvline(today, color = 'black', ls = '-')
	colors = ['tab:blue', 'tab:orange', 'tab:green', 'tab:red', 'tab:purple', 'tab:brown', 'tab:pink', 'tab:gray', 'tab:olive', 'tab:cyan']
	for i in range(totalProjects):
		j = totalProjects - i - 1
		if len(core_projects.teams[j]) != 0:
			projectStart = pd.Timestamp(core_projects.teams[j][0].get("created_at"))
		else:
			projectStart = pd.Timestamp(core_projects.created_at[j])
		if (core_projects.status[j] != 'finished'):
			projectEnd = pd.Timestamp(today)
		else:
			projectEnd = pd.Timestamp(core_projects.updated_at[j])
		#print(f"Start: {projectStart}, End: {projectEnd}")
		color = colorPicker(colors, core_projects.project[j]);
		gnt.broken_barh(
			[(projectStart, projectEnd - projectStart)],
			(basePosY - barWidth // 2, barWidth),
			facecolors = color,
			alpha = 0.7,
			edgecolor = ['black'],
			linestyle = 'dashed')
		basePosY = basePosY + 20

	legend_elements = [
	Patch(facecolor = colors[0], edgecolor = 'black', label = 'Algorithms'),
	Patch(facecolor = colors[3], edgecolor = 'black', label = 'Graphics'),
	Patch(facecolor = colors[4], edgecolor = 'black', label = 'Unix Dev.'),
	Patch(facecolor = colors[1], edgecolor = 'black', label = 'Sys. Admin. / Dev. Ops.'),
	Patch(facecolor = colors[6], edgecolor = 'black', label = 'Web Dev.'),
	Patch(facecolor = colors[7], edgecolor = 'black', label = 'Exams')
	]

	gnt.legend(handles = legend_elements, title = "Project Theme", loc='upper center', bbox_to_anchor=(0.5, -0.05),
	fancybox=True, shadow=True, ncol=6)

	gnt.annotate('Today', (today + pd.Timedelta(days = 2), (figureH // 4) * 3),
			fontsize = 16,
			horizontalalignment = 'left', verticalalignment = 'bottom')

	return fig

def buildMetrics(core_projects, code_reviews, today):
	totalHours = 1700
	sumHours = sumStudyHours(core_projects)
	startDate = pd.Timestamp(core_projects.created_at[core_projects.shape[0] - 1])
	remainingHours = totalHours - sumHours
	if (remainingHours < 0):
		remainingHours = 0
	elapsedTime = today - startDate
	studyHours = pd.Timedelta(hours = sumHours)
	paceScore = studyHours / elapsedTime
	paceRating = 'None'
	if ("Being" in core_projects.project[0]):
		paceRating = 'Unicorn!'
		paceEmoji = '🦄'
	elif (paceScore > 0.20):
		paceRating = 'Peregrine falcon!'
		paceEmoji = '🦅'
	elif (paceScore > 0.15 and paceScore <= 0.20):
		paceRating = 'Thoroughbred!'
		paceEmoji = '🐎'
	elif (paceScore > 0.10 and paceScore <= 0.15):
		paceRating = 'Dodo!'
		paceEmoji = '🦤'
	elif (paceScore > 0.05 and paceScore <= 0.10):
		paceRating = 'Racoon!'
		paceEmoji = '🦝'
	elif (paceScore <= 0.05):
		paceRating = 'Sloth!'
		paceEmoji = '🦥'
	totalProjects = 21
	completeProjects = core_projects[core_projects.status == 'finished'].shape[0]
	remainingProjects = totalProjects - completeProjects
	if ("Being" in core_projects.project[0]):
		totalProjects = 4
		completeProjects = 0
		remainingProjects = totalProjects - completeProjects
	predictions = [
		"drink milk right from the carton",
		"have a celebrity crush",
		"pick their nose",
		"pick up a penny on the sidewalk",
		"tell a bad joke",
		"trip on their own feet",
		"cause the next pandemic",
		"eat pet food",
		"use misplaced emojis",
		"give bad advice",
		"talk back at their boss",
		"be the first one skinny dipping",
		"end up on a reality show",
		"win the lottery",
		"survive a zombie apocalypse",
		"forget their own birthday",
		"be late to their own wedding",
		"be a race car driver",
		"adopt a wild animal",
		"win the Nobel prize",
		"live on a beach",
		"take their exam intoxicated",
		"break a world record at something",
		"be a world traveler",
		"give someone the same gift twice",
		"become a millionaire",
		"break a Guinness world record",
		"be the richest person on planet",
		"work at a horse farm",
		"end up working a circus",
		"eat desert before dinner",
		"become a rockstar",
		"get lost in their own hometown",
		"eat something off the ground",
		"give their kid a ridiculous name",
		"lock themselves out of the house",
		"become a politician",
		"go to space",
		"mess up a job interview",
		"get a PhD",
		"become a philanthropist",
		"go bankrupt",
		"retire in the countryside",
		"lie on their CV",
		"live in the wild",
		"climb Everest",
		"become a pirate",
		"marry themselves",
		"say the wrong name at the altar",
		"to be in a commercial",
		"be a stingy parent",
		"indulge in late-night snacking",
		"become addicted to m&m's",
		"end the world",
		"rule the world",
		"be a horrible boss",
		"go to jail",
		"win a fart contest",
		"become a superhero",
		"rob a bank",
		"join a gang",
		"live for a hundred years",
		"get struck by lightning",
		"become a conspiracy theorist",
		"fail miserably at Tetris"
		]
	for i in range(code_reviews.shape[0]):
		code_reviews.loc[i, 'created_at'] = pd.Timestamp(code_reviews.loc[i, 'created_at'])
	code_reviews = code_reviews.loc[code_reviews["created_at"] > startDate]
	totalDefenses = code_reviews[code_reviews.reason == 'Earning after defense'].shape[0]
	totalEvaluations = code_reviews[code_reviews.reason == 'Defense plannification'].shape[0]
	totalCodeReviews = totalEvaluations + totalDefenses
	col1, col2, col3, col4, col5 = st.columns(5)
	col1.metric("Hours of Study", f"{sumHours}", f"{remainingHours} left", delta_color = "off")
	if ("Being" in core_projects.project[0]):
		col2.metric("Rarity", f"{paceEmoji}", f"As rare as a {paceRating}", delta_color = "off")
	else:
		col2.metric("Pace", f"{paceEmoji}", f"As quick as a {paceRating}", delta_color = "off")
	col3.metric("Project Validations", f"{completeProjects}", f"{remainingProjects} to go", delta_color = "off")
	col4.metric("Code Reviews", f"{totalCodeReviews}", f"with {totalDefenses} as the reviewer", delta_color = "off")
	col5.metric("Prediction", "Most likely to", f"{predictions[user_id % (len(predictions) - 1)]}", delta_color = "off")

if __name__ == '__main__':
	page_config()
	api = ic
	bar = st.progress(0)
	with st.form("Student info"):
		with st.sidebar:
			login = st.text_input('Intra name', '')
			passphrase = st.text_input('Passphrase', '')
			submitted = st.form_submit_button("Submit")
		if submitted and login and passphrase.lower() == "thanks for all the fish":
			user_id, user_name = getUserID(api, login)
			#LOG.debug(user_id)
			#LOG.debug(user_name)
			bar.progress(20)
			if user_id != 0:
				projects = getProjects(api, user_id)
				if not projects.empty:
					bar.progress(50)
					core_projects = getCoreProjects(projects)
					today = getToday()
					bar.progress(60)
					core_projects = squishCPP(core_projects, today)
					bar.progress(80)
					fig = buildGantt(32, 12, core_projects, today)
					bar.progress(90)
					st.header(f"{user_name}'s Core Curriculum Stats")
					try:
						eval_points = api.pages_threaded(f"https://api.intra.42.fr/v2/users/{user_id}/correction_point_historics")
						if eval_points:
							code_reviews = pd.DataFrame(eval_points)
							buildMetrics(core_projects, code_reviews, today)
							st.pyplot(fig)
							bar.progress(100)
						else:
							st.error("Evaluation data is not available!")
					except:
						st.error("An error occurred when fetching evaluation data. Please try again in a few minutes.")
			else:
				st.error("User not found")
