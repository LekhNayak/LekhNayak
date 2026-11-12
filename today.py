import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # 'Andrew6rant'
MAX_COMMIT_LOC = 10000 # commits changing more lines than this are left out of the LOC stats
# First line of the cache file. Changing it (e.g. after changing MAX_COMMIT_LOC) forces a full LOC recount.
CACHE_MARKER = 'LOC cache v2: commits over ' + str(MAX_COMMIT_LOC) + ' changed lines excluded\n'
QUERY_COUNT = {'user_getter': 0, 'token_check': 0,'follower_getter': 0, 'graph_repo_count': 0, 'graph_stars': 0, 'graph_merged_prs': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(start_date):
    """
    Returns the duration of coding experience since the given start_date.
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), start_date)
    return '{} {}, {} {}, {} {}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days))


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def graphql_request(query, variables, retries=6):
    """
    POST to GitHub's GraphQL API, retrying with exponential backoff on
    transient failures. Handles every failure mode the API can throw:
      - HTTP 429/5xx and connection errors (retry)
      - HTTP 200 with 'errors' in the body (retry)
      - secondary rate limits via the Retry-After header (wait it out)
      - primary rate limit exhaustion, via X-RateLimit-Reset (sleep until
        the hourly reset, so the run continues instead of crashing)
    Returns the response object on success, otherwise raises an Exception.
    """
    last_error = 'unknown error'
    for attempt in range(1, retries + 1):
        request = None
        try:
            request = requests.post('https://api.github.com/graphql',
                                    json={'query': query, 'variables': variables},
                                    headers=HEADERS, timeout=120)
        except requests.exceptions.RequestException as e:
            last_error = 'connection error: ' + str(e)

        ok = False
        if request is not None:
            status = request.status_code
            if status == 200:
                try:
                    body = request.json()
                except ValueError:
                    body = None
                if body is None or 'errors' in body:
                    last_error = 'GraphQL errors in response body'
                    if body is not None and 'errors' in body:
                        errs = [str(e.get('type', '')) + ' ' + str(e.get('message', '?'))[:200] for e in body['errors']]
                        last_error += ': ' + str(errs[:2])
                        # Rate limit exhausted: sleep until the hourly reset,
                        # then retry without burning a retry attempt.
                        if any(str(e.get('type', '')).upper() == 'RATE_LIMITED' for e in body['errors']):
                            reset = request.headers.get('X-RateLimit-Reset')
                            if reset:
                                try:
                                    wait = max(5, int(reset) - int(time.time()) + 2)
                                    print('GraphQL rate limit exhausted; sleeping ' + str(wait) + 's until reset')
                                    time.sleep(wait)
                                    continue
                                except ValueError:
                                    pass
                        # Any other GraphQL error (missing permission, restricted field, bad query)
                        # comes back the same every time, so give up after one retry instead of
                        # waiting through the full ~5 minute backoff.
                        if attempt >= 2:
                            break
                else:
                    ok = True
            else:
                last_error = 'HTTP ' + str(status)
                # Primary rate limit exhausted: sleep until reset, then retry.
                if status == 403 and request.headers.get('X-RateLimit-Remaining') == '0':
                    reset = request.headers.get('X-RateLimit-Reset')
                    if reset:
                        try:
                            wait = max(5, int(reset) - int(time.time()) + 2)
                            print('Primary rate limit exhausted; sleeping ' + str(wait) + 's until reset')
                            time.sleep(wait)
                            continue
                        except ValueError:
                            pass
        if ok:
            _pace_requests(request)
            return request

        wait = 5 * (2 ** (attempt - 1))
        if request is not None and 'Retry-After' in request.headers:
            try:
                wait = max(wait, int(request.headers['Retry-After']) + 2)
            except ValueError:
                pass
        print('GraphQL request failed (' + last_error + '); retrying in ' + str(wait) + 's (attempt ' + str(attempt) + '/' + str(retries) + ')')
        time.sleep(wait)
    raise Exception('GraphQL request failed after ' + str(attempt) + ' attempts (' + last_error + ')')


def _pace_requests(request):
    """
    After a successful request, pause briefly if the rate limit is nearly
    exhausted so the tail of the run (stars, repos, followers) isn't cut off.
    """
    remaining = request.headers.get('X-RateLimit-Remaining')
    reset = request.headers.get('X-RateLimit-Reset')
    if remaining is None or reset is None:
        return
    try:
        remaining = int(remaining)
        reset = int(reset)
    except ValueError:
        return
    if remaining < 10:
        sleep_for = max(0, reset - int(time.time())) + 2
        print('Rate limit low (' + str(remaining) + ' remaining); sleeping ' + str(sleep_for) + 's until reset')
        time.sleep(sleep_for)


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    return graphql_request(query, variables)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
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
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repo_count(owner_affiliation):
    """
    Uses GitHub's GraphQL v4 API to return how many repositories I have for the given affiliations.
    Asks for no per-repository fields, so it only needs basic read access.
    """
    query_count('graph_repo_count')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!) {
        user(login: $login) {
            repositories(first: 1, ownerAffiliations: $owner_affiliation) {
                totalCount
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME}
    request = simple_request(graph_repo_count.__name__, query, variables)
    return int(request.json()['data']['user']['repositories']['totalCount'])


def graph_stars(owner_affiliation):
    """
    Uses GitHub's GraphQL v4 API to return the total stars across my repositories, all pages.
    Reads each repository's stargazerCount number. The old query read stargazers { totalCount },
    which goes through the stargazer list; GitHub restricts that list for personal access tokens,
    and the error took the repo counts down with it.
    """
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                nodes {
                    stargazerCount
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    total, cursor = 0, None
    while True:
        query_count('graph_stars')
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        repos = simple_request(graph_stars.__name__, query, variables).json()['data']['user']['repositories']
        total += sum(node['stargazerCount'] for node in repos['nodes'] if node)
        if not repos['pageInfo']['hasNextPage']:
            return total
        cursor = repos['pageInfo']['endCursor']


def rest_repos_stars(username):
    """
    Fallback for when GraphQL fails: repository and star counts from the REST API.
    Only sees public repositories, so it is used only when the GraphQL queries fail.
    """
    repos, stars, page = 0, 0, 1
    while True:
        request = requests.get('https://api.github.com/users/' + username + '/repos',
                               params={'type': 'owner', 'per_page': 100, 'page': page},
                               headers=HEADERS, timeout=60)
        request.raise_for_status()
        batch = request.json()
        repos += len(batch)
        stars += sum(repo.get('stargazers_count', 0) for repo in batch)
        if len(batch) < 100:
            return repos, stars
        page += 1


def graph_merged_prs():
    """
    Uses GitHub's GraphQL v4 API to return how many pull requests I authored that were merged,
    in any repository (mine or other people's).
    """
    query_count('graph_merged_prs')
    query = '''
    query ($login: String!) {
        user(login: $login) {
            pullRequests(first: 1, states: MERGED) {
                totalCount
            }
        }
    }'''
    request = simple_request(graph_merged_prs.__name__, query, {'login': USER_NAME})
    return int(request.json()['data']['user']['pullRequests']['totalCount'])


def rest_merged_prs(username):
    """
    Fallback for when GraphQL fails: merged pull request count from the REST search API (public PRs only).
    """
    request = requests.get('https://api.github.com/search/issues',
                           params={'q': 'author:' + username + ' type:pr is:merged', 'per_page': 1},
                           headers=HEADERS, timeout=60)
    request.raise_for_status()
    return int(request.json()['total_count'])


def cached_repo_count(comment_size):
    """
    Fallback for the Contributed count: the LOC cache has one line per repository
    across the OWNER, COLLABORATOR and ORGANIZATION_MEMBER affiliations.
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'r') as f:
        return sum(1 for line in f.readlines()[comment_size:] if len(line.split()) >= 5)


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
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
    try:
        request = graphql_request(query, variables) # retries rate limits, timeouts, and 5xx errors
    except Exception:
        force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
        raise
    if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
        return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
    return 0


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time)
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        try:
            author_user = node['node']['author']['user']
        except (TypeError, KeyError):
            author_user = None
        if author_user == OWNER_ID:
            my_commits += 1
            # Huge commits are almost always datasets, venvs or generated files, not hand-written code
            if node['node']['additions'] + node['node']['deletions'] <= MAX_COMMIT_LOC:
                addition_total += node['node']['additions']
                deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
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
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if comment_size > 0 and data[:1] != [CACHE_MARKER]: # cache was built with different LOC rules: recount everything
        comment = [CACHE_MARKER] + data[1:comment_size]
        comment += ['This line is a comment block. Write whatever you want here.\n'] * (comment_size - len(comment))
        with open(filename, 'w') as f:
            f.writelines(comment)
        data = comment
        force_cache = True

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        try:
            parts = data[index].split()
            repo_hash = parts[0]
        except (IndexError, ValueError): # missing or malformed cache line
            repo_hash = ''
        expected = hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        if repo_hash != expected: # new repo or corrupt line: reset it to zero
            repo_hash = expected
            data[index] = repo_hash + ' 0 0 0 0\n'
            commit_count = 0
        else:
            try:
                commit_count = int(parts[1])
            except (IndexError, ValueError):
                commit_count = 0
                data[index] = repo_hash + ' 0 0 0 0\n'
        try:
            try:
                total = edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']
            except TypeError: # empty repository
                total = 0
            if commit_count != total:
                # if commit count has changed, update loc for that repo
                owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                loc = recursive_loc(owner, repo_name, data, cache_comment)
                data[index] = repo_hash + ' ' + str(total) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
        except Exception as e: # One failing repo shouldn't abort the whole run
            print('Skipping', edges[index]['node']['nameWithOwner'], 'for this run:', e)
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        try:
            loc = line.split()
            if len(loc) >= 5:
                loc_add += int(loc[3])
                loc_del += int(loc[4])
        except ValueError:
            continue
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data, pr_data=None):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, merged PRs, and lines written
    Values that are None are left untouched (the SVG keeps its current text).
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    if age_data is not None: justify_format(root, 'age_data', age_data, 37)
    if commit_data is not None: justify_format(root, 'commit_data', commit_data, 12)
    if star_data is not None: justify_format(root, 'star_data', star_data, 7)
    if repo_data is not None: justify_format(root, 'repo_data', repo_data, 4)
    if contrib_data is not None: justify_format(root, 'contrib_data', contrib_data)
    if follower_data is not None: justify_format(root, 'follower_data', follower_data, 8)
    if pr_data is not None: justify_format(root, 'pr_data', pr_data, 9) # right-aligns under the Commits value
    if loc_data is not None:
        if loc_data[2] != '-': justify_format(root, 'loc_data', loc_data[2], 12)
        if loc_data[0] != '-': justify_format(root, 'loc_add', loc_data[0])
        if loc_data[1] != '-': justify_format(root, 'loc_del', loc_data[1])
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    if length == 0: # no justification for this element
        return
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_string = ' ' # always keep at least one space after the key
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        try:
            total_commits += int(line.split()[2])
        except (IndexError, ValueError):
            continue
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
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


def token_check():
    """
    Prints which account the ACCESS_TOKEN belongs to, when it expires, and how many
    private repositories it can see, so token problems show up plainly in the Action log.
    """
    query_count('token_check')
    query = '''
    query ($login: String!) {
        viewer {
            login
            repositories(first: 1, ownerAffiliations: OWNER, privacy: PRIVATE) { totalCount }
        }
        user(login: $login) {
            repositories(first: 1, ownerAffiliations: OWNER, privacy: PRIVATE) { totalCount }
        }
    }'''
    request = simple_request(token_check.__name__, query, {'login': USER_NAME})
    data = request.json()['data']
    print('Token check: belongs to', data['viewer']['login'],
          '| expires', request.headers.get('github-authentication-token-expiration', 'never'),
          '| private repos visible:', data['user']['repositories']['totalCount'],
          '(as viewer:', str(data['viewer']['repositories']['totalCount']) + ')')
    if data['viewer']['repositories']['totalCount'] == 0:
        print('   -> This token sees no private repos. Give it "All repositories" access and save it as the ACCESS_TOKEN secret.')


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    Adapted from Andrew Grant's (Andrew6rant) original today.py for a terminal-style profile README.
    Each stat is computed independently: if one fails (rate limits, bad data, etc.), the run continues
    and writes whatever it did manage to fetch, instead of crashing and updating nothing.
    """
    print('Calculation times:')

    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'Andrew6rant'
    OWNER_ID = None
    acc_date = None
    user_time = 0
    try:
        user_data, user_time = perf_counter(user_getter, USER_NAME)
        OWNER_ID, acc_date = user_data
        formatter('account data', user_time)
    except Exception as e:
        print('Could not fetch account data (continuing without it):', e)
    try:
        token_check()
    except Exception as e:
        print('Token check failed (continuing):', e)

    age_data = None
    age_time = 0
    try:
        # CODING_START (YYYY-MM-DD) is set in .github/workflows/main.yml
        coding_start = datetime.datetime.strptime(os.environ.get('CODING_START', '2023-01-01'), '%Y-%m-%d')
        age_data, age_time = perf_counter(daily_readme, coding_start)
        formatter('age calculation', age_time)
    except Exception as e:
        print('Could not calculate age (continuing without it):', e)

    total_loc = ['-', '-', '-']
    loc_time = 0
    try:
        total_loc_raw, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
        if total_loc_raw is None:
            raise Exception('loc_query returned no data')
        total_loc = [str(v) for v in total_loc_raw[:3]]
    except Exception as e:
        print('LOC calculation failed (continuing with cached values):', e)
    formatter('LOC (cached)', loc_time) if total_loc[-1] != '-' else formatter('LOC (no cache)', loc_time)

    commit_data = None
    commit_time = 0
    try:
        commit_data, commit_time = perf_counter(commit_counter, 7)
    except Exception as e:
        print('Could not count commits (continuing without it):', e)
    formatter('commits', commit_time)

    repo_data = None
    try:
        repo_data = graph_repo_count(['OWNER'])
    except Exception as e:
        print('Could not fetch repo count from GraphQL:', e)
    star_data = None
    try:
        star_data = graph_stars(['OWNER'])
    except Exception as e:
        print('Could not fetch stars from GraphQL:', e)
    if repo_data is None or star_data is None:
        try:
            rest_repos, rest_stars = rest_repos_stars(USER_NAME)
            print('Using REST API fallback (public repos only): repos =', rest_repos, ', stars =', rest_stars)
            if repo_data is None: repo_data = rest_repos
            if star_data is None: star_data = rest_stars
        except Exception as e:
            print('REST fallback also failed (leaving repos/stars unchanged):', e)
    contrib_data = None
    try:
        contrib_data = graph_repo_count(['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    except Exception as e:
        print('Could not fetch contributed repo count from GraphQL:', e)
        try:
            contrib_data = cached_repo_count(7)
            print('Using LOC cache fallback: contributed =', contrib_data)
        except Exception as e:
            print('Cache fallback also failed (leaving contributed unchanged):', e)
    pr_data = None
    try:
        pr_data = graph_merged_prs()
    except Exception as e:
        print('Could not fetch merged PRs from GraphQL:', e)
        try:
            pr_data = rest_merged_prs(USER_NAME)
            print('Using REST search fallback (public PRs only): merged PRs =', pr_data)
        except Exception as e:
            print('REST fallback also failed (leaving merged PRs unchanged):', e)
    print('   repos:', repo_data, '| contributed:', contrib_data, '| stars:', star_data, '| merged PRs:', pr_data)
    follower_data = None
    try:
        follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    except Exception as e:
        print('Could not fetch followers (continuing without it):', e)

    if total_loc != ['-', '-', '-']:
        for index in range(3): total_loc[index] = '{:,}'.format(int(total_loc[index]))

    try:
        svg_overwrite('profile.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc, pr_data)
    except Exception as e:
        print('Could not write SVG files:', e)
        raise

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
