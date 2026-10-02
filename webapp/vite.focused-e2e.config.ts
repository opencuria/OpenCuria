import { mergeConfig, defineConfig } from 'vite'
import base from './vite.config'
import focused from '../e2e/fixtures/focused-api-config'

export default mergeConfig(base, defineConfig(focused))
