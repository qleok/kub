# ImagePullBackOff

## Image pull retry backoff
Source: https://kubernetes.io/docs/concepts/containers/images/#imagepullbackoff

`ImagePullBackOff` means the kubelet could not fetch the image needed to start the container and is waiting longer between retries. Check the original pull failure for a clue such as an invalid image reference or missing credentials for a private registry.

Action: Run `kubectl describe pod <pod> -n <namespace>` and read the failed image pull event for the underlying error.

## Private registry authentication and pull secrets
Source: https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/#create-a-pod-that-uses-your-secret

An `unauthorized` image pull points to registry authentication or access. A Pod can supply credentials through `imagePullSecrets`, which references a registry Secret in the Pod's namespace.

Action: Verify that the Pod references a Secret in its namespace with credentials that can pull from the target registry repository.

## Missing image tag or manifest
Source: https://kubernetes.io/docs/concepts/containers/images/#image-names

If the pull error says `manifest unknown`, check that the referenced image was published under the exact registry hostname, repository, and tag or digest. Omitting both a tag and a digest makes Kubernetes use the `latest` tag.

Action: Compare the Pod's full `image` value with a tag or digest that exists in the registry.
