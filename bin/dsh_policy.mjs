// Explicit write grants, registered as a normal native sandbox-policy service.
import {createRequire} from 'node:module'
import {realpathSync} from 'node:fs'
import {isAbsolute} from 'node:path'
import {pathToFileURL} from 'node:url'

export function scopedPolicy(Base, roots, mode) {
  if (!Array.isArray(roots) || roots.some(p => typeof p !== 'string' || !isAbsolute(p)))
    throw new Error('Assigned native write roots must be absolute directories.')
  const paths = [...new Set(roots.map(p => realpathSync(p)))]
  if (mode === 'workspace-write' && !paths.length) throw new Error('Explicit write roots are required.')
  return class extends Base {
    resolve(request = {}) {
      const policy = super.resolve(request)
      if (policy.mode === 'read-only') return policy
      if (mode !== 'workspace-write' || policy.mode !== 'workspace-write')
        throw new Error('Native mode exceeds the assigned file permissions.')
      return {...policy, workspaceRoot:paths[0], additionalWritableRoots:paths.slice(1)}
    }
  }
}

export async function apply(ctx, config) {
  const require = createRequire(realpathSync(config.moduleEntry))
  const {SandboxPolicyService} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-sandbox-policy')))
  await ctx.plugin(scopedPolicy(SandboxPolicyService, config.writeRoots, config.mode), config).await()
}
