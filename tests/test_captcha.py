import json
import os

import matplotlib.pyplot as plt
import numpy as np

from pytok import captcha_solver


def main():
    this_dir_path = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(this_dir_path, 'captcha_examples.json'), 'r') as f:
        data = json.load(f)
    for _puzzle_type, examples in data.items():
        for example in examples:
            puzzle_b64 = example['puzzle'].strip("b'")
            piece_b64 = example['piece'].strip("b'")

            best_angle = captcha_solver.whirl_solver(puzzle_b64, piece_b64)
            puzzle, piece, puzzle_edge, piece_edge = captcha_solver._get_images_and_edges(puzzle_b64, piece_b64)

            solved_puzzle = puzzle.copy()
            puzzle_r = (piece.shape[0] / 2) - 1
            for y in range(solved_puzzle.shape[1]):
                for x in range(solved_puzzle.shape[0]):
                    if (x - solved_puzzle.shape[0] / 2) ** 2 + (y - solved_puzzle.shape[1] / 2) ** 2 < puzzle_r ** 2:
                        theta = np.arctan2(y - solved_puzzle.shape[1] / 2, x - solved_puzzle.shape[0] / 2)
                        theta -= (best_angle / piece_edge.shape[0]) * 2 * np.pi
                        r = np.sqrt((x - solved_puzzle.shape[0] / 2) ** 2 + (y - solved_puzzle.shape[1] / 2) ** 2)
                        solved_puzzle[x, y] = piece[int(piece.shape[0] / 2 + r * np.cos(theta)), int(piece.shape[1] / 2 + r * np.sin(theta))]

            matches = np.zeros(puzzle_edge.shape[0])
            for angle in range(puzzle_edge.shape[0]):
                match = np.sum(puzzle_edge * np.roll(piece_edge, angle, axis=0))
                matches[angle] = match

            # save the best match
            fig, ax = plt.subplots(nrows=7)
            ax[0].imshow(puzzle)
            ax[1].imshow(piece)
            ax[2].imshow(solved_puzzle)
            ax[3].imshow(np.repeat(puzzle_edge[np.newaxis, :, :] / 255, 50, axis=0))
            ax[4].imshow(np.repeat(piece_edge[np.newaxis, :, :] / 255, 50, axis=0))
            ax[5].imshow(np.repeat(np.roll(piece_edge / 255, best_angle, axis=0)[np.newaxis, :, :], 50, axis=0))
            ax[6].plot(matches)
            plt.show()


if __name__ == '__main__':
    main()
