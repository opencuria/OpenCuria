/** Git REST bindings shared by the webapp and mobile client. */

import { createGitApi } from '@opencuria/client/workspaceTools'
import { getApiClient } from './api'

export const {
  repos: getGitRepos,
  repo: getGitRepo,
  history: getGitHistory,
  commitDetails: getGitCommitDetails,
  operation: runGitOperation,
  commitMessage: generateGitCommitMessage,
  fileContent: readGitFileContent,
} = createGitApi(getApiClient)
