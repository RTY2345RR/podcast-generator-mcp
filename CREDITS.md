# Credits

## Voice engine
Host voices are rendered and cloned by the [Miragea](https://miragea.ai) voice engine (https://miragea.ai/v1).

## Demo reference clips (examples/two-host-podcast-demo)
The two demo hosts are cloned from the CMU ARCTIC speech database:
- Leo (male): `cmu_us_bdl_arctic/wav/arctic_a0003.wav`
- Maya (female): `cmu_us_slt_arctic/wav/arctic_a0003.wav`
Source: http://festvox.org/cmu_arctic/

CMU ARCTIC licence notice (from http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_slt_arctic/COPYING; the same notice ships with the bdl voice). The demo uses the clips only as voice-cloning references; the clips themselves are not redistributed here.

```text
This voice is free for use for any purpose (commercial or otherwise)
subject to the pretty light restrictions detailed below.

                     Carnegie Mellon University
                         Copyright (c) 2003
                        All Rights Reserved.

 Permission to use, copy, modify,  and licence this software and its
 documentation for any purpose, is hereby granted without fee,
 subject to the following conditions:
  1. The code must retain the above copyright notice, this list of
     conditions and the following disclaimer.
  2. Any modifications must be clearly marked as such.
  3. Original authors' names are not deleted.

 THE AUTHORS OF THIS WORK DISCLAIM ALL WARRANTIES WITH REGARD TO
 THIS SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
 AND FITNESS, IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY
 SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
 AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
 ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF
 THIS SOFTWARE.

 See http://www.festvox.org/cmu_arctic/ for more details
```

## Tools
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT)
- [httpx](https://www.python-httpx.org/) (BSD-3-Clause)
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (BSD-2-Clause) and [FFmpeg](https://ffmpeg.org/) (LGPL/GPL, run as a separate program)

No code was copied from other podcast generators; see the comparison in the README.
