# Why this exists

I built HearAble for one person.

A close friend of mine is hard of hearing. Italian broadcasters caption the
evening news and not much else — live regional programmes, sport, talk shows,
most of daytime arrive with nothing at all, or with captions so far behind that
they describe a sentence which finished half a minute ago. In practice that
means sitting in front of a television watching people move their mouths.

So the first audience for this project is exactly one man, in one living room,
in front of one television. Everything here was decided by whether it helped
him: the subtitles are two lines because two lines read as one block from across
a room; there is one button because there should be one button; the mini PC
switches itself off because he should never have to think about it.

That part is done. It works, he uses it every evening, and if this repository
had stopped there it would still have been worth the months.

---

## But I hope it does not stop there

I do not think my friend is unusual. Tens of millions of people in Europe live
with meaningful hearing loss and a television schedule that is captioned in
patches. That gap is not waiting on a research breakthrough. It has been waiting
on somebody noticing that the pieces are now cheap enough to put together at
home.

The genuinely reusable idea in here is small, and it is not mine — it is just an
observation nobody seems to have acted on:

> **The audio is already inside your set-top box, in perfect quality, ahead of
> the picture.** Any DVB receiver running Linux will hand it to you through a
> standard PID filter, without a tuner, without a re-encode, and without touching
> the video path.

Everything downstream of that is ordinary engineering, and it now fits on a
fanless box that costs less than a games console. That is the part I would like
somebody else to take.

## What you might do with it

**Use it.** If you have an Enigma2 receiver and a spare mini PC, `DEPLOY.md`
will get you there. It will not be effortless — it was built for two specific
machines — but nothing in it is exotic.

**Use it for more than live television.** The same chain subtitles a film
played from the receiver's own storage — measured, eight checks, all passing.
Nothing in it is specific to broadcast.

**Point it at another language.** Nothing in the audio path knows or cares what
language is being spoken. Swap the model and the Italian word list and the rest
should hold. I have not tried it, and I would like to know.

**Try it on hardware I have never touched.** The tap is standard Linux DVB, so
it ought to work on receivers far beyond the one box I own. If you try, the two
measurements I would most like back are the ones from *your* box: how long until
the first byte comes out of `DMX_OUT_TSDEMUX_TAP`, and how far ahead of
`AUDIO_GET_PTS` it runs. On mine: 0.03 s and 64 ms.

**Tell me what it costs you.** This was built against one receiver and one mini
PC, and every number here is from those two. What it takes on different hardware
is the thing I cannot find out on my own.

## What this is not

It is not a product, and I am not promising support. It is one household's
system, documented well enough that somebody else could build their own, written
down honestly enough that you can tell what was measured from what was merely
hoped.

If it helps one more person hear their television, it has done more than I set
out to do.

---

*By Nunzio Raciti, with Claude (Opus) as development assistant.*
