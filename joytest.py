"""Controller diagnostic — not part of the game, just a probe.

    python joytest.py

Uses the low-level pygame.joystick API (not the SDL2 GameController wrapper,
which only recognizes devices already in SDL's mapping database — a real
pad can be invisible there while still working fine at this level). Opens
every connected joystick and prints every single raw event live, unfiltered
— no assumptions about which axis/button/hat index means what, no deadzone
hiding, no scripted prompts to press things in order. Press one button at a
time and read what comes out. Ctrl+C to quit.
"""
import pygame

pygame.init()
pygame.joystick.init()

count = pygame.joystick.get_count()
print(f"{count} joystick(s) detected", flush=True)
if count == 0:
    print(
        "Nothing detected at the pygame/SDL level. That means the OS isn't "
        "exposing it as an input device at all (yet) — check on the Linux "
        "side with:\n"
        "  ls /dev/input/js* /dev/input/by-id/ 2>/dev/null\n"
        "  cat /proc/bus/input/devices\n"
        "and see if an entry shows up for the pad.",
        flush=True,
    )
    raise SystemExit(1)

joysticks = []
for i in range(count):
    js = pygame.joystick.Joystick(i)
    js.init()
    joysticks.append(js)
    print(
        f"  [{i}] {js.get_name()}  guid={js.get_guid()}  "
        f"buttons={js.get_numbuttons()} axes={js.get_numaxes()} hats={js.get_numhats()}",
        flush=True,
    )

print("\nPress buttons one at a time. Every raw event prints below, unfiltered. Ctrl+C to quit.\n", flush=True)

clock = pygame.time.Clock()
running = True
while running:
    clock.tick(60)
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.JOYBUTTONDOWN:
            print(f"BUTTON DOWN  button={event.button}", flush=True)
        elif event.type == pygame.JOYBUTTONUP:
            print(f"BUTTON UP    button={event.button}", flush=True)
        elif event.type == pygame.JOYHATMOTION:
            print(f"HAT          hat={event.hat} value={event.value}", flush=True)
        elif event.type == pygame.JOYAXISMOTION:
            print(f"AXIS         axis={event.axis} value={event.value:.3f}", flush=True)
        elif event.type == pygame.JOYBALLMOTION:
            print(f"BALL         ball={event.ball} rel={event.rel}", flush=True)
        elif event.type == pygame.JOYDEVICEREMOVED:
            print("Controller disconnected", flush=True)
            running = False

pygame.quit()
