//! Extra mutators that are specific to a target.

use std::borrow::Cow;
use std::marker::PhantomData;

use libafl::{
    inputs::ResizableMutator,
    mutators::{MutationResult, Mutator},
};
use libafl_bolts::{Error, Named};

/// Mutator that appends `\r\n` after the inner mutator changes an input.
pub struct FtpLightMutator<M, I, S>
where
    M: Mutator<I, S>,
{
    name: Cow<'static, str>,
    inner: M,
    phantom: PhantomData<(I, S)>,
}

impl<M, I, S> FtpLightMutator<M, I, S>
where
    M: Mutator<I, S> + Named,
{
    /// Wrap `mutator`.
    pub fn new(mutator: M) -> Self {
        let name = Cow::Owned(format!("FtpLightMutator[{}]", mutator.name()));
        Self {
            name,
            inner: mutator,
            phantom: PhantomData,
        }
    }
}

impl<M, I, S> Mutator<I, S> for FtpLightMutator<M, I, S>
where
    M: Mutator<I, S>,
    I: ResizableMutator<u8>,
{
    fn mutate(&mut self, state: &mut S, input: &mut I) -> Result<MutationResult, Error> {
        match self.inner.mutate(state, input)? {
            MutationResult::Mutated => {
                input.extend(&[b'\r', b'\n']);
                Ok(MutationResult::Mutated)
            }
            skipped => Ok(skipped),
        }
    }

    fn post_exec(&mut self, state: &mut S, new_corpus_id: Option<libafl::corpus::CorpusId>) -> Result<(), Error> {
        self.inner.post_exec(state, new_corpus_id)
    }
}

impl<M, I, S> Named for FtpLightMutator<M, I, S>
where
    M: Mutator<I, S>,
{
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}
