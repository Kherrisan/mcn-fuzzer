//! HTTP wrapper around an inner byte mutator.

use std::borrow::Cow;
use std::marker::PhantomData;

use libafl::{
    inputs::ResizableMutator,
    mutators::{MutationResult, Mutator},
};
use libafl_bolts::{Error, Named};

/// Appends `\r\n\r\n` after the inner mutator changes an input.
pub struct HttpMutator<M, I, S>
where
    M: Mutator<I, S>,
{
    name: Cow<'static, str>,
    inner: M,
    phantom: PhantomData<(I, S)>,
}

impl<M, I, S> HttpMutator<M, I, S>
where
    M: Mutator<I, S> + Named,
{
    /// Wrap `mutator`.
    pub fn new(mutator: M) -> Self {
        let name = Cow::Owned(format!("HttpMutator[{}]", mutator.name()));
        Self {
            name,
            inner: mutator,
            phantom: PhantomData,
        }
    }
}

impl<M, I, S> Mutator<I, S> for HttpMutator<M, I, S>
where
    M: Mutator<I, S>,
    I: ResizableMutator<u8>,
{
    fn mutate(&mut self, state: &mut S, input: &mut I) -> Result<MutationResult, Error> {
        match self.inner.mutate(state, input)? {
            MutationResult::Mutated => {
                input.extend(&[b'\r', b'\n', b'\r', b'\n']);
                Ok(MutationResult::Mutated)
            }
            skipped => Ok(skipped),
        }
    }

    fn post_exec(&mut self, state: &mut S, new_corpus_id: Option<libafl::corpus::CorpusId>) -> Result<(), Error> {
        self.inner.post_exec(state, new_corpus_id)
    }
}

impl<M, I, S> Named for HttpMutator<M, I, S>
where
    M: Mutator<I, S>,
{
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}
