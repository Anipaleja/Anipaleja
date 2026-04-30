import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Environment variables required:
# ACCESS_TOKEN = GitHub personal access token
# USER_NAME = your GitHub username (e.g. "anipaleja")
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']

QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0
}


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    )


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    request = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS
    )
    if request.status_code == 200:
        return request
    raise Exception(func_name, 'failed:', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {
        'start_date': start_date,
        'end_date': end_date,
        'login': USER_NAME
    }
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        nameWithOwner
                        stargazers {
                            totalCount
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {
        'owner_affiliation': owner_affiliation,
        'login': USER_NAME,
        'cursor': cursor
    }
    request = simple_request(graph_repos_stars.__name__, query, variables)

    if count_type == 'repos':
        return request.json()['data']['user']['repositories']['totalCount']
    elif count_type == 'stars':
        return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment,
                  addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges {
                                node {
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS
    )

    if request.status_code == 200:
        repo = request.json()['data']['repository']
        if repo['defaultBranchRef'] is None:
            return 0

        history = repo['defaultBranchRef']['target']['history']

        for node in history['edges']:
            if node['node']['author']['user'] == OWNER_ID:
                my_commits += 1
                addition_total += node['node']['additions']
                deletion_total += node['node']['deletions']

        if not history['pageInfo']['hasNextPage']:
            return addition_total, deletion_total, my_commits

        return recursive_loc(
            owner, repo_name, data, cache_comment,
            addition_total, deletion_total, my_commits,
            history['pageInfo']['endCursor']
        )

    force_close_file(data, cache_comment)
    raise Exception('recursive_loc failed:', request.status_code)


def loc_query(owner_affiliation, comment_size=0, cursor=None, edges=None):
    if edges is None:
        edges = []

    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''

    variables = {
        'owner_affiliation': owner_affiliation,
        'login': USER_NAME,
        'cursor': cursor
    }

    request = simple_request(loc_query.__name__, query, variables)
    data = request.json()['data']['user']['repositories']

    edges += data['edges']

    if data['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, comment_size, data['pageInfo']['endCursor'], edges)

    return cache_builder(edges, comment_size)


def cache_builder(edges, comment_size, loc_add=0, loc_del=0):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode()).hexdigest() + '.txt'

    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) != len(edges):
        flush_cache(edges, filename)

    with open(filename, 'r') as f:
        data = f.readlines()

    for index, edge in enumerate(edges):
        repo_hash = hashlib.sha256(edge['node']['nameWithOwner'].encode()).hexdigest()
        try:
            stored = data[index].split()
            if stored[0] != repo_hash:
                raise ValueError

            if int(stored[1]) != edge['node']['defaultBranchRef']['target']['history']['totalCount']:
                owner, repo = edge['node']['nameWithOwner'].split('/')
                loc = recursive_loc(owner, repo, data, [])
                data[index] = f"{repo_hash} {edge['node']['defaultBranchRef']['target']['history']['totalCount']} {loc[2]} {loc[0]} {loc[1]}\n"

        except:
            data[index] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w') as f:
        f.writelines(data)

    for line in data:
        _, _, _, add, delete = line.split()
        loc_add += int(add)
        loc_del += int(delete)

    return [loc_add, loc_del, loc_add - loc_del, True]


def flush_cache(edges, filename):
    with open(filename, 'w') as f:
        for node in edges:
            repo_hash = hashlib.sha256(node['node']['nameWithOwner'].encode()).hexdigest()
            f.write(f"{repo_hash} 0 0 0 0\n")


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode()).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)


def stars_counter(data):
    return sum(node['node']['stargazers']['totalCount'] for node in data)


def commit_counter():
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode()).hexdigest() + '.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    return sum(int(line.split()[2]) for line in data)


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    result = funct(*args)
    return result, time.perf_counter() - start


if __name__ == '__main__':
    print('Calculation times:')

    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, _ = user_data

    # Set YOUR birthday here
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2008, 1, 1))

    total_loc, _ = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    commit_data, _ = perf_counter(commit_counter)
    star_data, _ = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, _ = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, _ = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, _ = perf_counter(follower_getter, USER_NAME)

    print("Done.")
